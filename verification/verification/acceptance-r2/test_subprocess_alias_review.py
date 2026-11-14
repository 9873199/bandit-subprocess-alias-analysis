import pytest
from bandit.core.config import BanditConfig
from bandit.core.manager import BanditManager

PREFIX='import subprocess as sp\nfrom subprocess import run as r\n'
CASES=[
 ('direct-module','sp.run("echo ok", shell=True)',1),
 ('direct-import','r("echo ok", shell=True)',1),
 ('simple','f=sp.run\nf("echo ok", shell=True)',1),
 ('chained','f=sp.run\ng=f\nh=g\nh("echo ok", shell=True)',1),
 ('tuple','f,g=(sp.run,sp.check_call)\nf("echo ok", shell=True)\ng("echo ok", shell=True)',2),
 ('import-alias','f=r\nf("echo ok", shell=True)',1),
 ('closure','f=sp.run\ndef outer():\n def inner():\n  f("echo ok", shell=True)\n return inner',1),
 ('global','f=sp.run\ndef call():\n global f\n f("echo ok", shell=True)',1),
 ('nonlocal','def outer():\n f=sp.run\n def inner():\n  nonlocal f\n  f("echo ok", shell=True)\n return inner',1),
 ('function-arg','f=sp.run\ndef call(f):\n f("echo ok", shell=True)',0),
 ('local-before-assign','f=sp.run\ndef call():\n f("echo ok", shell=True)\n f=print',0),
 ('local-before-def','f=sp.run\ndef call():\n f("echo ok", shell=True)\n def f(*a,**k): pass',0),
 ('rebind','f=sp.run\nf=print\nf("echo ok", shell=True)',0),
 ('delete','f=sp.run\ndel f\nf("echo ok", shell=True)',0),
 ('import-shadow','f=sp.run\nimport logging as f\nf("echo ok", shell=True)',0),
 ('module-shadow','def call(sp):\n sp.run("echo ok", shell=True)',0),
 ('branch-ambiguous','if condition:\n f=sp.run\nelse:\n f=print\nf("echo ok", shell=True)',0),
 ('loop-ambiguous','f=sp.run\nfor x in things:\n f=print\nf("echo ok", shell=True)',0),
 ('dynamic-attr','f=getattr(sp, name)\nf("echo ok", shell=True)',0),
 ('class-scope','class C:\n f=sp.run\n f("echo ok", shell=True)',1),
 ('method-no-class-closure','class C:\n f=sp.run\n def call(self):\n  f("echo ok", shell=True)',0),
 ('method-no-module-shadow','f=sp.run\nclass C:\n f=print\n def call(self):\n  f("echo ok", shell=True)',1),
 ('not-shell','f=sp.run\nf("echo ok", shell=False)',0),
]
CASES.extend([
 ('local-before-import','f=sp.run\ndef call():\n f("echo ok", shell=True)\n import logging as f',0),
 ('exception-local-binding','f=sp.run\ndef call():\n f("echo ok", shell=True)\n try: operation()\n except Exception as f: pass',0),
 ('comprehension-shadow','f=sp.run\nvalues=[f("echo ok", shell=True) for f in funcs]',0),
 ('outer-after-comprehension','f=sp.run\nvalues=[f for f in funcs]\nf("echo ok", shell=True)',1),
 ('nonlocal-rebind','def outer():\n f=sp.run\n def inner():\n  nonlocal f\n  f=print\n  f("echo ok", shell=True)',0),
 ('global-rebind','f=sp.run\ndef call():\n global f\n f=print\n f("echo ok", shell=True)',0),
])
@pytest.mark.parametrize('label,body,expected',CASES,ids=[c[0] for c in CASES])
def test_resolve_alias_without_executing_code(tmp_path,label,body,expected):
    path=tmp_path/'sample.py';path.write_text(PREFIX+body+'\n',encoding='utf8')
    manager=BanditManager(BanditConfig(), 'file', quiet=True)
    manager.discover_files([str(path)],recursive=False)
    manager.run_tests()
    issues=[i for i in manager.results if i.test_id=='B602']
    assert len(issues)==expected, [(i.lineno,i.text) for i in issues]
    for issue in issues:
        assert 'shell=True' in path.read_text().splitlines()[issue.lineno-1]
