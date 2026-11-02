import csv
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from edr_monitor import load_codes, match_companies, parse_zip, canonical_name


def subject(code, name, state='зареєстровано', record='1', short=''):
    import xml.sax.saxutils as sx
    return '<SUBJECT>' + ''.join(f'<{tag}>{sx.escape(value)}</{tag}>' for tag, value in [
        ('EDRPOU', code), ('NAME', name), ('SHORT_NAME', short),
        ('STAN', state), ('RECORD', record)]) + '</SUBJECT>'


class MatchTests(unittest.TestCase):
    def test_codes_are_text(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'codes.txt'
            p.write_text('02004717\n02004717\n580309097\n', encoding='utf-8')
            self.assertEqual(load_codes(p), ['02004717','580309097'])

    def test_normalization_and_unique_exact_only(self):
        self.assertEqual(canonical_name('КНП «Хмельницька обласна лікарня»'),
                         canonical_name('кнп "хмельницька обласна лікарня"'))
        records = {
            '02004717': {'Found':'YES','SelectionNote':'SINGLE_RECORD',
                         'OfficialName':'КНП "ХМЕЛЬНИЦЬКА ОБЛАСНА ЛІКАРНЯ"','ShortName':''},
            '12345678': {'Found':'YES','SelectionNote':'SINGLE_RECORD',
                         'OfficialName':'ТОВ "СПІЛЬНА НАЗВА"','ShortName':''},
        }
        liga = [
            {'LigaId':'id1','LigaName':'КНП «Хмельницька обласна лікарня»','SourceUrl':'https://x/1'},
            {'LigaId':'id2','LigaName':'ТОВ СПІЛЬНА НАЗВА','SourceUrl':'https://x/2'},
            {'LigaId':'id3','LigaName':'ТОВ «СПІЛЬНА НАЗВА»','SourceUrl':'https://x/3'},
            {'LigaId':'id4','LigaName':'ТОВ "НЕВІДОМА КОМПАНІЯ"','SourceUrl':'https://x/4'},
        ]
        result, review = match_companies(liga, records)
        self.assertEqual(result[0]['EDRPOU'], '02004717')
        self.assertEqual(result[0]['MatchStatus'], 'AUTO_EXACT')
        self.assertEqual(result[1]['MatchStatus'], 'REVIEW_SHARED_CODE')
        self.assertFalse(result[1]['EDRPOU'])
        self.assertEqual(result[2]['MatchStatus'], 'REVIEW_SHARED_CODE')
        self.assertEqual(result[3]['MatchStatus'], 'REVIEW_NO_EXACT_MATCH')
        self.assertEqual(len(review), 3)

    def test_streamed_zip_and_duplicate_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.zip'
            xml = '<ROOT xmlns="urn:test">' + ''.join([
                subject('02004717', 'КНП "ХМЕЛЬНИЦЬКА ОБЛАСНА ЛІКАРНЯ"'),
                subject('12345678', 'ТОВ ДВА', state='припинено', record='1'),
                subject('12345678', 'ТОВ ДВА', state='зареєстровано', record='2'),
            ]) + '</ROOT>'
            with zipfile.ZipFile(path, 'w') as zf:
                zf.writestr('UO.xml', xml.encode('utf-8'))
            result, count = parse_zip(path, ['02004717','12345678','00000000'])
            self.assertEqual(count, 3)
            self.assertEqual(result['02004717']['OfficialName'], 'КНП "ХМЕЛЬНИЦЬКА ОБЛАСНА ЛІКАРНЯ"')
            self.assertEqual(result['12345678']['SelectionNote'], 'MULTIPLE_RECORDS')
            self.assertEqual(result['12345678']['Record'], '2')
            self.assertEqual(result['00000000']['Found'], 'NO')

    def test_offline_cli_and_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'codes.txt').write_text('02004717\n', encoding='utf-8')
            with (root/'liga.csv').open('w',encoding='utf-8',newline='') as file:
                writer=csv.DictWriter(file,fieldnames=['LigaId','LigaName','SourceUrl'])
                writer.writeheader()
                writer.writerow({'LigaId':'uuid1','LigaName':'ТОВ "ТЕСТ"','SourceUrl':'https://a/b'})
            with zipfile.ZipFile(root/'sample.zip','w') as archive:
                archive.writestr('UO.xml', '<ROOT>'+subject('02004717','ТОВ ТЕСТ')+'</ROOT>')
            cmd = [sys.executable, str(ROOT/'scripts'/'edr_monitor.py'),
                   '--codes', str(root/'codes.txt'), '--liga', str(root/'liga.csv'),
                   '--zip', str(root/'sample.zip'), '--output', str(root/'out')]
            proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
            summary = json.loads((root/'out'/'match_summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['auto_exact_unique_matches'],1)
            with (root/'out'/'companycodes_all.csv').open(encoding='utf-8-sig') as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]['EDRPOU'],'02004717')
            self.assertEqual(rows[0]['MatchStatus'],'AUTO_EXACT')


if __name__ == '__main__':
    unittest.main()
