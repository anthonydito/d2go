#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved


"""
API for exporting a pytorch model to a predictor, the predictor contains model(s) in
deployable format and predefined functions as glue code. The exported predictor should
generate same output as the original pytorch model. (See predictor/api.py for details of
predictor)

This API defines customizable methods for the pytorch model:
    prepare_for_export (required by the default export_predictor): returns
        PredictorExportConfig which tells information about how export the predictor.

NOTE:
    1: There's a difference between predictor type and model type. model type
        refers to predefined deployable format such as caffe2, torchscript(_int8),
        while the predictor type can be anything that "export_predictor" can
        recognize.
    2: The standard model exporting methods are provided by the library code, they're
        meant to be modularized and can be used by customized export_predictor as well.
"""

import json
import logging
import os
import sys
from typing import Callable, Dict, NamedTuple, Optional, Tuple, Union

import torch
import torch.nn as nn
from d2go.export.api import ModelExportMethod, ModelExportMethodRegistry
from d2go.quantization.modeling import post_training_quantize
from detectron2.utils.file_io import PathManager
from mobile_cv.arch.utils import fuse_utils
from mobile_cv.predictor.api import FuncInfo, ModelInfo, PredictorInfo
from mobile_cv.predictor.builtin_functions import (
    IdentityPostprocess,
    IdentityPreprocess,
    NaiveRunFunc,
)

TORCH_VERSION: Tuple[int, ...] = tuple(int(x) for x in torch.__version__.split(".")[:2])
if TORCH_VERSION > (1, 10):
    from torch.ao.quantization import convert
    from torch.ao.quantization.quantize_fx import convert_fx
else:
    from torch.quantization import convert
    from torch.quantization.quantize_fx import convert_fx

logger = logging.getLogger(__name__)


def convert_predictor(
    cfg,
    pytorch_model,
    predictor_type,
    data_loader,
):
    if "int8" in predictor_type:
        if not cfg.QUANTIZATION.QAT.ENABLED:
            logger.info(
                "The model is not quantized during training, running post"
                " training quantization ..."
            )

            pytorch_model = post_training_quantize(cfg, pytorch_model, data_loader)
            # only check bn exists in ptq as qat still has bn inside fused ops
            if fuse_utils.check_bn_exist(pytorch_model):
                logger.warn("Post training quantized model has bn inside fused ops")
        logger.info(f"Converting quantized model {cfg.QUANTIZATION.BACKEND}...")

        if hasattr(pytorch_model, "prepare_for_quant_convert"):
            pytorch_model = pytorch_model.prepare_for_quant_convert(cfg)
        else:
            # TODO(T93870381): move this to a default function
            if cfg.QUANTIZATION.EAGER_MODE:
                pytorch_model = convert(pytorch_model, inplace=False)
            else:  # FX graph mode quantization
                pytorch_model = convert_fx(pytorch_model)

        logger.info("Quantized Model:\n{}".format(pytorch_model))
    else:
        pytorch_model = fuse_utils.fuse_model(pytorch_model)
        logger.info("Fused Model:\n{}".format(pytorch_model))
        if fuse_utils.count_bn_exist(pytorch_model) > 0:
            logger.warning("BN existed in pytorch model after fusing.")
    return pytorch_model


def convert_and_export_predictor(
    cfg,
    pytorch_model,
    predictor_type,
    output_dir,
    data_loader,
):
    """
    Entry point for convert and export model. This involves two steps:
        - convert: converting the given `pytorch_model` to another format, currently
            mainly for quantizing the model.
        - export: exporting the converted `pytorch_model` to predictor. This step
            should not alter the behaviour of model.
    """
    pytorch_model = convert_predictor(cfg, pytorch_model, predictor_type, data_loader)
    return export_predictor(cfg, pytorch_model, predictor_type, output_dir, data_loader)


def export_predictor(cfg, pytorch_model, predictor_type, output_dir, data_loader):
    """
    Interface for exporting a pytorch model to predictor of given type. This function
    can be override to achieve customized exporting procedure, eg. using non-default
    optimization passes, composing traced models, etc.

    Args:
        cfg (CfgNode): the config
        pytorch_model (nn.Module): a pytorch model, mostly also a meta-arch
        predictor_type (str): a string which specifies the type of predictor, note that
            the definition of type is interpreted by "export_predictor", the default
            implementation uses the deployable model format (eg. caffe2_fp32,
            torchscript_int8) as predictor type.
        output_dir (str): the parent directory where the predictor will be saved
        data_loader: data loader for the pytorch model

    Returns:
        predictor_path (str): the directory of exported predictor, a sub-directory of
            "output_dir"
    """
    return default_export_predictor(
        cfg, pytorch_model, predictor_type, output_dir, data_loader
    )


def _export_single_model(
    predictor_path,
    model,
    input_args,
    save_path,
    model_export_method,
    model_export_kwargs,
):
    assert isinstance(model, nn.Module), model
    # model_export_method either inherits ModelExportMethod or is a key in the registry
    model_export_method_str = None
    if isinstance(model_export_method, str):
        model_export_method_str = model_export_method
        model_export_method = ModelExportMethodRegistry.get(model_export_method)
    assert issubclass(model_export_method, ModelExportMethod), model_export_method
    logger.info("Using model export method: {}".format(model_export_method))

    load_kwargs = model_export_method.export(
        model=model,
        input_args=input_args,
        save_path=save_path,
        export_method=model_export_method_str,
        **model_export_kwargs,
    )
    assert isinstance(load_kwargs, dict)
    model_rel_path = os.path.relpath(save_path, predictor_path)
    return ModelInfo(
        path=model_rel_path,
        export_method="{}.{}".format(
            model_export_method.__module__, model_export_method.__qualname__
        ),
        load_kwargs=load_kwargs,
    )


def default_export_predictor(
    cfg, pytorch_model, predictor_type, output_dir, data_loader
):
    # The default implementation acts based on the PredictorExportConfig returned by
    # calling "prepare_for_export". It'll export all sub models in standard way
    # according to the "predictor_type".
    assert hasattr(pytorch_model, "prepare_for_export"), pytorch_model
    inputs = next(iter(data_loader))
    export_config = pytorch_model.prepare_for_export(cfg, inputs, predictor_type)
    model_inputs = (
        export_config.data_generator(inputs)
        if export_config.data_generator is not None
        else (inputs,)
    )

    predictor_path = os.path.join(output_dir, predictor_type)
    PathManager.mkdirs(predictor_path)

    predictor_init_kwargs = {
        "preprocess_info": export_config.preprocess_info,
        "postprocess_info": export_config.postprocess_info,
        "run_func_info": export_config.run_func_info,
    }

    if isinstance(export_config.model, dict):
        models_info = {}
        for name, model in export_config.model.items():
            save_path = os.path.join(predictor_path, name)
            model_info = _export_single_model(
                predictor_path=predictor_path,
                model=model,
                input_args=model_inputs[name] if model_inputs is not None else None,
                save_path=save_path,
                model_export_method=(
                    predictor_type
                    if export_config.model_export_method is None
                    else export_config.model_export_method[name]
                ),
                model_export_kwargs=(
                    {}
                    if export_config.model_export_kwargs is None
                    else export_config.model_export_kwargs[name]
                ),
            )
            models_info[name] = model_info
        predictor_init_kwargs["models"] = models_info
    else:
        save_path = predictor_path  # for single model exported files are put under `predictor_path` together with predictor_info.json
        model_info = _export_single_model(
            predictor_path=predictor_path,
            model=export_config.model,
            input_args=model_inputs,
            save_path=save_path,
            model_export_method=export_config.model_export_method or predictor_type,
            model_export_kwargs=export_config.model_export_kwargs or {},
        )
        predictor_init_kwargs["model"] = model_info

    # assemble predictor
    predictor_info = PredictorInfo(**predictor_init_kwargs)
    with PathManager.open(
        os.path.join(predictor_path, "predictor_info.json"), "w"
    ) as f:
        json.dump(predictor_info.to_dict(), f, indent=4)

    return predictor_path
