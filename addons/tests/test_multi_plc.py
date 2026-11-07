import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import multi_plc as mp

SLN = 'Microsoft Visual Studio Solution File\r\nProject("{B1E792BE-AA5F-4E3C-8C82-674BF9C0715B}") = "I", "I\\I.tsproj", "{1}"\r\nEndProject\r\n'
TSPROJ = (
    '<?xml version="1.0"?>\r\n<TcSmProject>\r\n<Project>\r\n<Plc>\r\n'
    '<Project GUID="{a}" Name="Inline" PrjFilePath="Inline\\Inline.plcproj" ReloadTmc="false">\r\n</Project>\r\n'
    '<Project Name="Ext1" TmcFileName="e1.tmc">\r\n<Instance TmcPath="..\\e1.tmc"/>\r\n</Project>\r\n'
    '<Project Name="Ext2" ReloadTmc="false"><Instance TmcPath="..\\e2.tmc"/></Project>\r\n'
    '<Project Name="Ext3" ReloadTmc="true"><Instance TmcPath="..\\e3.tmc"/></Project>\r\n'
    '<Project File="Side.xti"/>\r\n'
    '</Plc>\r\n<Safety>\r\n<Project Name="Ext1" Other="x"/>\r\n</Safety>\r\n</Project>\r\n</TcSmProject>\r\n'
)
XTI = '<?xml version="1.0"?>\r\n<TcSmItem>\r\n<Project Name="Side" TmcFileName="s.tmc"><Instance TmcPath="..\\s.tmc"/></Project>\r\n</TcSmItem>\r\n'


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "I" / "_Config" / "PLC").mkdir(parents=True)
        (self.dir / "I.sln").write_bytes(SLN.encode())
        (self.dir / "I" / "I.tsproj").write_bytes(TSPROJ.encode())
        (self.dir / "I" / "_Config" / "PLC" / "Side.xti").write_bytes(XTI.encode())
        self.sln = self.dir / "I.sln"
        self.tsproj = self.dir / "I" / "I.tsproj"
        self.xti = self.dir / "I" / "_Config" / "PLC" / "Side.xti"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_preview_writes_nothing(self):
        report = mp.enable_tmc_auto_reload(self.sln)
        self.assertEqual(["Ext1", "Ext2", "Side"], [m["name"] for m in report["modified"]])
        self.assertEqual(["Ext3"], report["alreadySet"])
        self.assertEqual(TSPROJ.encode(), self.tsproj.read_bytes())
        self.assertEqual(XTI.encode(), self.xti.read_bytes())

    def test_apply_changes_only_the_tags(self):
        report = mp.enable_tmc_auto_reload(self.sln, apply=True)
        expected = (TSPROJ.replace('<Project Name="Ext1" TmcFileName="e1.tmc">', '<Project Name="Ext1" TmcFileName="e1.tmc" ReloadTmc="true">')
                    .replace('<Project Name="Ext2" ReloadTmc="false">', '<Project Name="Ext2" ReloadTmc="true">'))
        self.assertEqual(expected.encode(), self.tsproj.read_bytes())
        self.assertEqual(XTI.replace('TmcFileName="s.tmc">', 'TmcFileName="s.tmc" ReloadTmc="true">').encode(), self.xti.read_bytes())
        self.assertEqual(2, len(report["backups"]))
        self.assertEqual(TSPROJ.encode(), Path(next(b for b in report["backups"] if "tsproj" in b)).read_bytes())

    def test_embedded_only_on_request(self):
        report = mp.enable_tmc_auto_reload(self.sln, plc_names=["inline"], include_embedded=True, apply=True)
        self.assertEqual(["Inline"], [m["name"] for m in report["modified"]])
        self.assertIn(b'Name="Inline" PrjFilePath="Inline\\Inline.plcproj" ReloadTmc="true"', self.tsproj.read_bytes())
        self.assertIn(b'<Project Name="Ext1" TmcFileName="e1.tmc">', self.tsproj.read_bytes())

    def test_unknown_plc_is_refused(self):
        with self.assertRaises(ValueError):
            mp.enable_tmc_auto_reload(self.sln, plc_names=["Nope"])

    def test_safety_project_of_the_same_name_is_untouched(self):
        mp.enable_tmc_auto_reload(self.sln, plc_names=["Ext1"], apply=True)
        self.assertIn(b'<Project Name="Ext1" Other="x"/>', self.tsproj.read_bytes())

if __name__ == "__main__":
    unittest.main()
