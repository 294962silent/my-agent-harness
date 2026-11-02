import inspect

from trl import GRPOConfig, GRPOTrainer

print("=== GRPOConfig 支持的参数 ===")
sig = inspect.signature(GRPOConfig.__init__)
params = [p for p in sig.parameters if p not in ("self", "args", "kwargs")]
for p in params:
    print(" ", p)

print("\n=== GRPOTrainer 支持的参数 ===")
sig2 = inspect.signature(GRPOTrainer.__init__)
for p in sig2.parameters:
    if p not in ("self", "args", "kwargs"):
        print(" ", p)
