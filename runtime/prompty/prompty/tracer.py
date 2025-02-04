import os
import json
import inspect
import numbers
import traceback
import importlib
import contextlib
from pathlib import Path
from numbers import Number
from datetime import datetime
from pydantic import BaseModel
from functools import wraps, partial
from typing import Any, Callable, Dict, Iterator, List


# clean up key value pairs for sensitive values
def sanitize(key: str, value: Any) -> Any:
    if isinstance(value, str) and any(
        [s in key.lower() for s in ["key", "secret", "password", "credential"]]
    ):
        return 10 * "*"
    elif isinstance(value, dict):
        return {k: sanitize(k, v) for k, v in value.items()}
    else:
        return value


class Tracer:
    _tracers: Dict[str, Callable[[str], Iterator[Callable[[str, Any], None]]]] = {}

    SIGNATURE = "signature"
    INPUTS = "inputs"
    RESULT = "result"

    @classmethod
    def add(
        cls, name: str, tracer: Callable[[str], Iterator[Callable[[str, Any], None]]]
    ) -> None:
        cls._tracers[name] = tracer

    @classmethod
    def clear(cls) -> None:
        cls._tracers = {}

    @classmethod
    @contextlib.contextmanager
    def start(cls, name: str, attributes: Dict[str, Any] = None) -> Iterator[Callable[[str, Any], None]]:
        with contextlib.ExitStack() as stack:
            traces = [
                stack.enter_context(tracer(name)) for tracer in cls._tracers.values()
            ]

            if attributes:
                for trace in traces:
                    for key, value in attributes.items():
                        trace(key, value)

            yield lambda key, value: [
                # normalize and sanitize any trace values
                trace(key, sanitize(key, to_dict(value)))
                for trace in traces
            ]


def to_dict(obj: Any) -> Dict[str, Any]:
    # simple json types
    if isinstance(obj, str) or isinstance(obj, Number) or isinstance(obj, bool):
        return obj
    # datetime
    elif isinstance(obj, datetime):
        return obj.isoformat()
    # safe Prompty obj serialization
    elif type(obj).__name__ == "Prompty":
        return obj.to_safe_dict()
    # safe PromptyStream obj serialization
    elif type(obj).__name__ == "PromptyStream":
        return "PromptyStream"
    elif type(obj).__name__ == "AsyncPromptyStream":
        return "AsyncPromptyStream"
    # pydantic models have their own json serialization
    elif isinstance(obj, BaseModel):
        return obj.model_dump()
    # recursive list and dict
    elif isinstance(obj, list):
        return [to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: v if isinstance(v, str) else to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, Path):
        return str(obj)
    # cast to string otherwise...
    else:
        return str(obj)


def _name(func: Callable, args):
    if hasattr(func, "__qualname__"):
        signature = f"{func.__module__}.{func.__qualname__}"
    else:
        signature = f"{func.__module__}.{func.__name__}"

    # core invoker gets special treatment prompty.invoker.Invoker
    core_invoker = signature.startswith("prompty.invoker.Invoker.run")
    if core_invoker:
        name = type(args[0]).__name__
        if signature.endswith("async"):
            signature = (
                f"{args[0].__module__}.{args[0].__class__.__name__}.invoke_async"
            )
        else:
            signature = f"{args[0].__module__}.{args[0].__class__.__name__}.invoke"
    else:
        name = func.__name__

    return name, signature


def _inputs(func: Callable, args, kwargs) -> dict:
    ba = inspect.signature(func).bind(*args, **kwargs)
    ba.apply_defaults()

    inputs = {k: to_dict(v) for k, v in ba.arguments.items() if k != "self"}

    return inputs


def _results(result: Any) -> dict:
    return to_dict(result) if result is not None else "None"


def _trace_sync(
    func: Callable = None, **okwargs: Any
) -> Callable:

    @wraps(func)
    def wrapper(*args, **kwargs):
        name, signature = _name(func, args)
        altname: str = None
        # special case
        if "name" in okwargs:
            altname = name
            name = okwargs["name"]
            del okwargs["name"]

        with Tracer.start(name) as trace:
            if altname != None:
                trace("function", altname)

            trace("signature", signature)

            # support arbitrary keyword
            # arguments for trace decorator
            for k, v in okwargs.items():
                trace(k, to_dict(v))

            inputs = _inputs(func, args, kwargs)
            trace("inputs", inputs)

            try:
                result = func(*args, **kwargs)
                trace("result", _results(result))
            except Exception as e:
                trace(
                    "result",
                    {
                        "exception": {
                            "type": type(e),
                            "traceback": (
                                traceback.format_tb(tb=e.__traceback__)
                                if e.__traceback__
                                else None
                            ),
                            "message": str(e),
                            "args": to_dict(e.args),
                        }
                    },
                )
                raise e

            return result

    return wrapper


def _trace_async(
    func: Callable = None, **okwargs: Any
) -> Callable:

    @wraps(func)
    async def wrapper(*args, **kwargs):
        name, signature = _name(func, args)
        altname: str = None
        # special case
        if "name" in okwargs:
            altname = name
            name = okwargs["name"]
            del okwargs["name"]

        with Tracer.start(name) as trace:
            if altname != None:
                trace("function", altname)
                
            trace("signature", signature)

            # support arbitrary keyword
            # arguments for trace decorator
            for k, v in okwargs.items():
                trace(k, to_dict(v))

            inputs = _inputs(func, args, kwargs)
            trace("inputs", inputs)
            try:
                result = await func(*args, **kwargs)
                trace("result", _results(result))
            except Exception as e:
                trace(
                    "result",
                    {
                        "exception": {
                            "type": type(e),
                            "traceback": (
                                traceback.format_tb(tb=e.__traceback__)
                                if e.__traceback__
                                else None
                            ),
                            "message": str(e),
                            "args": to_dict(e.args),
                        }
                    },
                )
                raise e

            return result

    return wrapper


def trace(func: Callable = None, **kwargs: Any) -> Callable:
    if func is None:
        return partial(trace, **kwargs)
    wrapped_method = _trace_async if inspect.iscoroutinefunction(func) else _trace_sync
    return wrapped_method(func, **kwargs)


class PromptyTracer:
    def __init__(self, output_dir: str = None) -> None:
        if output_dir:
            self.output = Path(output_dir).resolve().absolute()
        else:
            self.output = Path(Path(os.getcwd()) / ".runs").resolve().absolute()

        if not self.output.exists():
            self.output.mkdir(parents=True, exist_ok=True)

        self.stack: List[Dict[str, Any]] = []

    @contextlib.contextmanager
    def tracer(self, name: str) -> Iterator[Callable[[str, Any], None]]:
        try:
            self.stack.append({"name": name})
            frame = self.stack[-1]
            frame["__time"] = {
                "start": datetime.now(),
            }

            def add(key: str, value: Any) -> None:
                if key not in frame:
                    frame[key] = value
                # multiple values creates list
                else:
                    if isinstance(frame[key], list):
                        frame[key].append(value)
                    else:
                        frame[key] = [frame[key], value]

            yield add
        finally:
            frame = self.stack.pop()
            start: datetime = frame["__time"]["start"]
            end: datetime = datetime.now()

            # add duration to frame
            frame["__time"] = {
                "start": start.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "duration": int((end - start).total_seconds() * 1000),
            }

            # hoist usage to parent frame
            if "result" in frame and isinstance(frame["result"], dict):
                if "usage" in frame["result"]:
                    frame["__usage"] = self.hoist_item(
                        frame["result"]["usage"],
                        frame["__usage"] if "__usage" in frame else {},
                    )

            # streamed results may have usage as well
            if "result" in frame and isinstance(frame["result"], list):
                for result in frame["result"]:
                    if (
                        isinstance(result, dict)
                        and "usage" in result
                        and isinstance(result["usage"], dict)
                    ):
                        frame["__usage"] = self.hoist_item(
                            result["usage"],
                            frame["__usage"] if "__usage" in frame else {},
                        )

            # add any usage frames from below
            if "__frames" in frame:
                for child in frame["__frames"]:
                    if "__usage" in child:
                        frame["__usage"] = self.hoist_item(
                            child["__usage"],
                            frame["__usage"] if "__usage" in frame else {},
                        )

            # if stack is empty, dump the frame
            if len(self.stack) == 0:
                self.write_trace(frame)
            # otherwise, append the frame to the parent
            else:
                if "__frames" not in self.stack[-1]:
                    self.stack[-1]["__frames"] = []
                self.stack[-1]["__frames"].append(frame)

    def hoist_item(self, src: Dict[str, Any], cur: Dict[str, Any]) -> None:
        for key, value in src.items():
            if value is None or isinstance(value, list) or isinstance(value, dict):
                continue
            try:
                if key not in cur:
                    cur[key] = value
                else:
                    cur[key] += value
            except:
                continue

        return cur

    def write_trace(self, frame: Dict[str, Any]) -> None:
        trace_file = (
            self.output
            / f"{frame['name']}.{datetime.now().strftime('%Y%m%d.%H%M%S')}.tracy"
        )

        v = importlib.metadata.version("prompty")
        enriched_frame = {
            "runtime": "python",
            "version": v,
            "trace": frame,
        }

        with open(trace_file, "w") as f:
            json.dump(enriched_frame, f, indent=4)


@contextlib.contextmanager
def console_tracer(name: str) -> Iterator[Callable[[str, Any], None]]:
    try:
        print(f"Starting {name}")
        yield lambda key, value: print(
            f"{key}:\n{json.dumps(to_dict(value), indent=4)}"
        )
    finally:
        print(f"Ending {name}")
