import inspect

from trl import GRPOConfig

print("=== 全部参数（含默认值）===")
sig = inspect.signature(GRPOConfig.__init__)
for name, p in sig.parameters.items():
    if name in ("self", "args", "kwargs"):
        continue
    if any(k in name.lower() for k in ("length", "generation", "prompt", "completion", "batch", "temperature", "steps")):
        print(f"  {name} = {p.default}")
