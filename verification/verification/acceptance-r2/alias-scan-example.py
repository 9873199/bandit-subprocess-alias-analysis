from pathlib import Path
import tempfile
from bandit.core.config import BanditConfig
from bandit.core.manager import BanditManager
samples=[('direct alias','import subprocess as sp\nf=sp.run\nf("echo ok",shell=True)\n',1),('local def after call','import subprocess as sp\nf=sp.run\ndef wrapper():\n f("echo ok",shell=True)\n def f(*args,**kwargs):pass\n',0),('parameter hides module','import subprocess as sp\ndef wrapper(sp):\n sp.run("echo ok",shell=True)\n',0)]
with tempfile.TemporaryDirectory(prefix='alias-example-') as folder:
 for label,code,expected in samples:
  path=Path(folder)/'example.py';path.write_text(code,encoding='utf8')
  manager=BanditManager(BanditConfig(),'file',quiet=True);manager.discover_files([str(path)]);manager.run_tests()
  findings=[i for i in manager.results if i.test_id=='B602']
  print(f'{label}: expected B602={expected}, observed={len(findings)}',flush=True)
  for i in findings:print(f'  B602 at line {i.lineno}, severity {i.severity}')
print('Only static scans; sample code was never executed.')
