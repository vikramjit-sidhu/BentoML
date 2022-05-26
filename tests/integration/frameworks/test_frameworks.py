from __future__ import annotations

import types
import typing as t

import pytest

import bentoml
from bentoml.exceptions import NotFound
from bentoml._internal.models.model import ModelContext
from bentoml._internal.models.model import ModelSignature
from bentoml._internal.runner.runner import Runner
from bentoml._internal.runner.resource import Resource
from bentoml._internal.runner.runner_handle.local import LocalRunnerRef

from .models import FrameworkTestModel


def test_wrong_module_load_exc(framework: types.ModuleType):
    with bentoml.models.create(
        "wrong_module",
        module=__name__,
        context=ModelContext("wrong_module", {"wrong_module": "1.0.0"}),
        signatures={},
    ) as ctx:
        tag = ctx.tag

    with pytest.raises(
        NotFound, match=f"Model wrong_module:.* was saved with module {__name__}, "
    ):
        framework.load_model(tag)


def test_generic_arguments(framework: types.ModuleType, test_model: FrameworkTestModel):
    # test that the generic save API works
    from sklearn.preprocessing import StandardScaler  # type: ignore (bad sklearn types)

    scaler: StandardScaler = StandardScaler().fit(  # type: ignore (bad sklearn types)
        [[4], [3], [7], [8], [4], [3], [9], [6]]
    )
    assert scaler.mean_[0] == 5.5  # type: ignore (bad sklearn types)
    assert scaler.var_[0] == 4.75  # type: ignore (bad sklearn types)

    kwargs = test_model.save_kwargs.copy()
    kwargs["signatures"] = {
        "pytest-signature-rjM5": {"batchable": True, "batch_dim": (1, 0)}
    }
    kwargs["labels"] = {
        "pytest-label-N4nr": "pytest-label-value-4mH7",
        "pytest-label-7q72": "pytest-label-value-3mDd",
    }
    kwargs["custom_objects"] = {"pytest-custom-object-r7BU": scaler}
    kwargs["metadata"] = {
        "pytest-metadata-vSW4": [0, 9, 2],
        "pytest-metadata-qJJ3": "Wy5M",
    }
    tag = framework.save_model(
        test_model.name,
        test_model.model,
        **kwargs,
    )

    bento_model: bentoml.Model = framework.get(tag)

    meth = "pytest-signature-rjM5"
    assert bento_model.info.signatures[meth] == ModelSignature.from_dict(kwargs["signatures"][meth])  # type: ignore
    assert bento_model.info.labels == kwargs["labels"]
    # print(bento_model.custom_objects)
    assert bento_model.custom_objects["pytest-custom-object-r7BU"].mean_[0] == 5.5
    assert bento_model.custom_objects["pytest-custom-object-r7BU"].var_[0] == 4.75
    assert bento_model.info.metadata == kwargs["metadata"]

    print(tag)


@pytest.fixture(name="saved_model")
def fixture_saved_model(
    framework: types.ModuleType, test_model: FrameworkTestModel
) -> bentoml.Tag:
    return framework.save_model(
        test_model.name, test_model.model, **test_model.save_kwargs
    )


def test_get(
    framework: types.ModuleType,
    test_model: FrameworkTestModel,
    saved_model: bentoml.Tag,
):
    # test that the generic get API works
    bento_model = framework.get(saved_model)

    assert bento_model.info.name == test_model.name

    bento_model_from_str = framework.get(str(saved_model))
    assert bento_model == bento_model_from_str


def test_get_runnable(
    framework: types.ModuleType,
    saved_model: bentoml.Tag,
):
    bento_model = framework.get(saved_model)

    runnable = framework.get_runnable(bento_model)

    assert isinstance(
        runnable, t.Type
    ), "get_runnable for {bento_model.info.name} does not return a type"
    assert issubclass(
        runnable, bentoml.Runnable
    ), "get_runnable for {bento_model.info.name} doesn't return a subclass of bentoml.Runnable"
    assert (
        len(runnable.methods) > 0
    ), "get_runnable for {bento_model.info.name} gives a runnable with no methods"


def test_load(
    framework: types.ModuleType,
    test_model: FrameworkTestModel,
    saved_model: bentoml.Tag,
):
    for configuration in test_model.configurations:
        model = framework.load_model(saved_model)

        configuration.check_model(model, Resource())

        for method, inps in configuration.test_inputs.items():
            for inp in inps:
                args = [inp.preprocess(arg) for arg in inp.input_args]
                kwargs = {
                    key: inp.preprocess(kwarg)
                    for key, kwarg in inp.input_kwargs.items()
                }
                out = getattr(model, method)(*args, **kwargs)
                inp.check_output(out)


def test_runner_batching(
    framework: types.ModuleType,
    test_model: FrameworkTestModel,
    saved_model: bentoml.Tag,
):
    from bentoml._internal.runner.utils import Params
    from bentoml._internal.runner.batching import batch_params
    from bentoml._internal.runner.container import AutoContainer

    bento_model = framework.get(saved_model)

    for config in test_model.configurations:
        runner = bento_model.with_options(**config.load_kwargs).to_runner()
        runner.init_local()
        for meth, inputs in config.test_inputs.items():
            if len(inputs) < 2:
                continue

            batch_dim = getattr(runner, meth).config.batch_dim
            paramss = [
                Params(*inp.input_args, **inp.input_kwargs).map(
                    # pylint: disable=cell-var-from-loop # lambda used before loop continues
                    lambda arg: AutoContainer.to_payload(arg, batch_dim=batch_dim[0])
                )
                for inp in inputs
            ]

            params, indices = batch_params(paramss, batch_dim[0])

            batch_res = getattr(runner, meth).run(*params.args, **params.kwargs)

            outps = AutoContainer.batch_to_payloads(batch_res, indices, batch_dim[1])

            for i, outp in enumerate(outps):
                inputs[i].check_output(AutoContainer.from_payload(outp))

        runner.destroy()


@pytest.mark.gpus
def test_runner_nvidia_gpu(
    framework: types.ModuleType,
    test_model: FrameworkTestModel,
    saved_model: bentoml.Tag,
):
    gpu_resource = Resource(nvidia_gpu=1.0)
    bento_model = framework.get(saved_model)

    for config in test_model.configurations:
        model_with_options = bento_model.with_options(**config.load_kwargs)

        runnable = framework.get_runnable(model_with_options)
        runner = Runner(runnable, nvidia_gpu=1)

        for meth, inputs in config.test_inputs.items():
            # TODO: use strategies to initialize GPU
            # strategy = DefaultStrategy()
            # strategy.setup_worker(runnable, gpu_resource)

            runner.init_local()

            runner_handle = t.cast(LocalRunnerRef, runner._runner_handle)
            runnable = runner_handle._runnable
            if hasattr(runnable, "model") and runnable.model is not None:
                config.check_model(runner, gpu_resource)

            for inp in inputs:
                outp = getattr(runner, meth).run(*inp.input_args, **inp.input_kwargs)
                inp.check_output(outp)

            runner.destroy()
