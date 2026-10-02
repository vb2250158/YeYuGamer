"""Exercise native UTF-8 JSON capture in the actual Windows PowerShell 5 host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell scheduled host')
class ScheduledDailyEncodingTests(unittest.TestCase):
    def test_scheduled_header_preserves_native_chinese_json(self):
        root=Path(__file__).resolve().parents[2]
        source=(root/'scripts/Invoke-YeYuGamerScheduledDaily.ps1').read_text(encoding='utf-8-sig')
        header='\n'.join(line for line in source.splitlines() if line.startswith('[Console]::OutputEncoding ='))
        with tempfile.TemporaryDirectory(prefix='yeyu-scheduled-encoding-') as temp:
            fixture=Path(temp)/'fixture.py'
            fixture.write_text("import json\nprint(json.dumps({'text':'每日任务正常','detail':'原神当前需要检查。'},ensure_ascii=False))\n",encoding='utf-8')
            quote=lambda value:"'"+str(value).replace("'","''")+"'"
            command=header+"\n$payload = & "+quote(sys.executable)+" -X utf8 "+quote(fixture)+" 2>&1 | Out-String\n"
            command+="$value = $payload | ConvertFrom-Json -ErrorAction Stop\n"
            command+="if ($value.text -ceq '每日任务正常' -and $value.detail -ceq '原神当前需要检查。') { [Console]::Out.WriteLine('preserved') } else { exit 8 }"
            shell=Path(os.environ['WINDIR'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
            result=subprocess.run([str(shell),'-NoProfile','-NonInteractive','-Command',command],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
            self.assertEqual(result.stdout.decode('utf-8').strip(),'preserved')


if __name__=='__main__': unittest.main()
