using System;
using System.IO;
using System.Linq;
using System.Security.AccessControl;
using System.Security.Principal;
using TcAutomation.Core;

static class LayoutTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }
    static bool Throws<T>(Action action) where T : Exception
    {
        try { action(); return false; } catch (T) { return true; }
    }

    // One PLC project on disk, referenced inline or through an independent project file.
    static string Fixture(string root, string tsprojPlcNode, string xti = null)
    {
        Directory.CreateDirectory(Path.Combine(root, "PLC1"));
        File.WriteAllText(Path.Combine(root, "PLC1", "PLC1.plcproj"),
            "<Project xmlns=\"http://schemas.microsoft.com/developer/msbuild/2003\"><ItemGroup><Compile Include=\"MAIN.TcPOU\" /></ItemGroup></Project>");
        File.WriteAllText(Path.Combine(root, "PLC1", "MAIN.TcPOU"), "<TcPlcObject />");
        File.WriteAllText(Path.Combine(root, "PLC1", "PLC1.tmc"), "<TcModuleClass><Modules><Module><DataAreas><DataArea><Symbol><Name>MAIN.n</Name></Symbol></DataArea></DataAreas></Module></Modules></TcModuleClass>");
        if (xti != null)
        {
            Directory.CreateDirectory(Path.Combine(root, "_Config", "PLC"));
            File.WriteAllText(Path.Combine(root, "_Config", "PLC", "PLC1.xti"), xti);
        }
        string tsproj = Path.Combine(root, "Qual.tsproj");
        File.WriteAllText(tsproj, "<TcSmProject><Project><Io><Project File=\"Device1.xti\" /></Io><Plc>" + tsprojPlcNode + "</Plc></Project></TcSmProject>");
        return tsproj;
    }

    public static void Run()
    {
        string directory = Path.Combine(Path.GetTempPath(), "twincat-layout-tests-" + Guid.NewGuid().ToString("N"));
        try
        {
            string inline = Fixture(Path.Combine(directory, "inline"),
                "<Project Name=\"PLC1\" PrjFilePath=\"PLC1\\PLC1.plcproj\" TmcFilePath=\"PLC1\\PLC1.tmc\" />");
            string independent = Fixture(Path.Combine(directory, "xti"), "<Project File=\"PLC1.xti\" />",
                "<TcSmItem><Project Name=\"PLC1\" PrjFilePath=\"..\\..\\PLC1\\PLC1.plcproj\" TmcFilePath=\"..\\..\\PLC1\\PLC1.tmc\" /></TcSmItem>");

            foreach (var (tsproj, layout) in new[] { (inline, "inline"), (independent, "independent .xti") })
            {
                string root = Path.GetDirectoryName(tsproj);
                var all = BuildArtifacts.Capture(tsproj);
                Check(all.Count == 1 && all[0].PlcName == "PLC1", layout + ": one PLC found");
                Check(all[0].ProjectPath == Path.Combine(root, "PLC1", "PLC1.plcproj"), layout + ": project path " + all[0].ProjectPath);
                Check(all[0].Path == Path.Combine(root, "PLC1", "PLC1.tmc") && all[0].Exists && all[0].SymbolCount == 1, layout + ": TMC resolved and read");
                Check(all[0].Error == null && all[0].SourceSha256 != null, layout + ": source manifest hashed");
                Check(BuildArtifacts.Capture(tsproj, "plc1").Single().PlcName == "PLC1", layout + ": selected PLC found");
                Check(Throws<ArgumentException>(() => BuildArtifacts.Capture(tsproj, "Other")), layout + ": unknown PLC rejected");
            }

            string xtiFile = Path.Combine(directory, "xti", "_Config", "PLC", "PLC1.xti");
            var gaps = new System.Collections.Generic.List<string>();
            File.WriteAllText(xtiFile, "<TcSmItem><Project");
            Check(BuildArtifacts.Capture(independent, null, gaps).Count == 0 && gaps.Single() == xtiFile, "Malformed independent file reported as a gap, not thrown");

            gaps.Clear();
            File.WriteAllText(xtiFile, "<TcSmItem />");
            FileSecurity security = null;
            var deny = new FileSystemAccessRule(WindowsIdentity.GetCurrent().User, FileSystemRights.ReadData, AccessControlType.Deny);
            try
            {
                security = File.GetAccessControl(xtiFile);
                security.AddAccessRule(deny);
                File.SetAccessControl(xtiFile, security);
            }
            catch (Exception ex) when (ex is NotSupportedException || ex is UnauthorizedAccessException || ex is PlatformNotSupportedException)
            {
                security = null; // No ACL support for the temp folder: skip only this check.
                Console.WriteLine("SKIP: access-denied layout check (" + ex.GetType().Name + ")");
            }
            if (security != null)
            {
                try { Check(BuildArtifacts.Capture(independent, null, gaps).Count == 0 && gaps.Single() == xtiFile, "Access-denied independent file reported as a gap, not thrown"); }
                finally { security.RemoveAccessRule(deny); File.SetAccessControl(xtiFile, security); }
            }

            gaps.Clear();
            File.Delete(xtiFile);
            Check(BuildArtifacts.Capture(independent, null, gaps).Count == 0 && gaps.Single() == xtiFile, "Missing independent file reported as a gap, not dropped");
            try { BuildArtifacts.Capture(independent, "PLC1"); Check(false, "Selected PLC in a missing file was found"); }
            catch (ArgumentException ex) { Check(ex.Message.Contains(xtiFile), "Error names the missing file: " + ex.Message); }

            string mixed = Fixture(Path.Combine(directory, "mixed"),
                "<Project Name=\"PLC1\" PrjFilePath=\"PLC1\\PLC1.plcproj\" TmcFilePath=\"PLC1\\PLC1.tmc\" /><Project File=\"PLC2.xti\" />");
            Check(BuildArtifacts.Capture(mixed, "PLC1").Single().PlcName == "PLC1", "A missing sibling file does not block the selected PLC");
            gaps.Clear();
            var mixedAll = BuildArtifacts.Capture(mixed, null, gaps);
            Check(mixedAll.Count == 1 && mixedAll[0].PlcName == "PLC1" && gaps.Count == 1, "Unfiltered capture returns only real PLCs; the sibling is a gap");

            // Present mixed layout: an inline PLC1 plus PLC2 in an .xti without TmcFilePath.
            string both = Path.Combine(directory, "both");
            Fixture(both, "", null);
            Directory.CreateDirectory(Path.Combine(both, "PLC2"));
            File.WriteAllText(Path.Combine(both, "PLC2", "PLC2.plcproj"), "<Project />");
            Directory.CreateDirectory(Path.Combine(both, "_Config", "PLC"));
            File.WriteAllText(Path.Combine(both, "_Config", "PLC", "PLC2.xti"), "<TcSmItem><Project Name=\"PLC2\" PrjFilePath=\"..\\..\\PLC2\\PLC2.plcproj\" /></TcSmItem>");
            string bothProj = Path.Combine(both, "Qual.tsproj");
            File.WriteAllText(bothProj, "<TcSmProject><Project><Plc><Project Name=\"PLC1\" PrjFilePath=\"PLC1\\PLC1.plcproj\" TmcFilePath=\"PLC1\\PLC1.tmc\" /><Project File=\"PLC2.xti\" /></Plc></Project></TcSmProject>");
            var pair = BuildArtifacts.Capture(bothProj);
            Check(pair.Count == 2 && pair.Select(a => a.PlcName).OrderBy(n => n).SequenceEqual(new[] { "PLC1", "PLC2" }), "Inline and independent PLCs found together");
            Check(pair.Single(a => a.PlcName == "PLC2").Path == Path.Combine(both, "PLC2", "PLC2.tmc"), "Missing TmcFilePath defaults next to the .plcproj");

            // Native batches decide stop-on-error from these helpers; anonymous receipts use lowercase names.
            Check(!StepDispatcher.IsResultSuccessful(new { success = false, errorMessage = "e" }), "Lowercase success:false is a failure");
            Check(StepDispatcher.ExtractErrorFromResult(new { success = false, errorMessage = "e" }) == "e", "Lowercase errorMessage is extracted");
        }
        finally
        {
            try { Directory.Delete(directory, true); } catch { }
        }
        Console.WriteLine("PASS: " + cases + " PLC project layout checks; no XAE or PLC used.");
    }
}
