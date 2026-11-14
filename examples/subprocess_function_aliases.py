"""True and false positive cases for subprocess function aliases (B602).

The assignment-aware name resolver must attribute calls made through
direct function aliases back to the configured subprocess functions,
while never reporting a name that merely happens to share one.

Lines marked ``TP`` must produce a B602 issue; lines marked ``FP`` must
not.  Other plugin IDs (B603/B604/B607 ...) may still apply.
"""
import subprocess as sp
from subprocess import Popen, run as r


# --- True positives: aliases that provably point at subprocess funcs ---

# simple direct alias after an aliased import
f = sp.run
f("/bin/ls", shell=True)  # TP B602

# alias of a from-imported, renamed function
g = r
g("/bin/ls", shell=True)  # TP B602

# multi-level alias chain
h = f
h("/bin/ls", shell=True)  # TP B602

# tuple assignment, positional matching
a, b = sp.run, sp.Popen
a("/bin/ls", shell=True)  # TP B602
b("/bin/ls", shell=True)  # TP B602

# list assignment, positional matching
[c, d] = sp.call, sp.check_call
c("/bin/ls", shell=True)  # TP B602
d("/bin/ls", shell=True)  # TP B602

# alias through a name proven to be the subprocess module
mod = sp
e = mod.run
e("/bin/ls", shell=True)  # TP B602

# the alias survives a subscript write and an attribute write
t = sp.run
storage = {}
storage["key"] = t
t.flag = 1
t("/bin/ls", shell=True)  # TP B602


# a closure that only reads the outer alias keeps the proven binding
def closure_outer():
    f = sp.run

    def inner():
        f("/bin/ls", shell=True)  # TP B602

    inner()


# agreeing branches both prove the same binding
if condition:  # noqa: F821
    n = sp.run
else:
    n = sp.run
n("/bin/ls", shell=True)  # TP B602


# a lambda without a shadowing parameter reads the outer alias
f = sp.run
lambda_caller = lambda: f("/bin/ls", shell=True)  # TP B602


# --- False positives: names that must not be treated as subprocess ---

# an ordinary function that happens to share the imported name
def run(cmd, shell=False):
    return None


run("/bin/ls", shell=True)  # FP B602 (B604 only)


# a function parameter shadows the alias
def param_shadow(run):
    run("/bin/ls", shell=True)  # FP B602


# a local rebinding cuts the outer alias
def local_rebind():
    f = lambda *args, **kwargs: None
    f("/bin/ls", shell=True)  # FP B602


# whole-function locality: the name is local even before its assignment
def use_before_assignment():
    f("/bin/ls", shell=True)  # FP B602
    f = sp.run


# a nested function that rebinds the name does not read the outer alias
def closure_shadow():
    f = sp.run

    def inner():
        f = lambda *args, **kwargs: None
        f("/bin/ls", shell=True)  # FP B602

    inner()


# a class-body binding is not visible inside its methods
class Worker:
    class_alias = sp.run

    def method(self):
        # ``class_alias`` only exists in the class body, which method
        # scopes cannot see, so this cannot be proven to be subprocess.
        class_alias("/bin/ls", shell=True)  # FP B602


# del removes the binding
deleted = sp.run
del deleted


# a later assignment to something unprovable invalidates the alias
j = sp.run
j = unknown_call_result  # noqa: F821
j("/bin/ls", shell=True)  # FP B602


# a subsequent import shadows the earlier assignment
k = sp.run
import os as k  # noqa: F811
k.system("/bin/ls")  # FP B602


# disagreeing branches leave the name unproven
if condition:  # noqa: F821
    m = sp.run
else:
    m = lambda *args, **kwargs: None
m("/bin/ls", shell=True)  # FP B602


# a loop may rebind the name, so nothing inside it is certain
for i in range(3):
    p = sp.run if i else other  # noqa: F821
    p("/bin/ls", shell=True)  # FP B602


# a call on the right hand side is not a proven function alias
z = sp.run(["/bin/true"])
z("/bin/ls", shell=True)  # FP B602


# global/nonlocal declarations cannot be proven through lexical lookup
G = sp.run


def uses_global():
    global G
    G("/bin/ls", shell=True)  # FP B602


def outer_nonlocal():
    f = sp.run

    def inner():
        nonlocal f
        f("/bin/ls", shell=True)  # FP B602

    inner()
