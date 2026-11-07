using System;
using TcAutomation.Commands;

static class LibraryInfoTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    // The refusal message, or null when the project passes.
    static string Refusal(string xml)
    {
        try { GenerateLibraryCommand.RequireLibraryInfo(xml); return null; }
        catch (InvalidOperationException ex) { return ex.Message; }
    }

    static string Project(string projectInfo) =>
        "<TreeItem><ItemName>PLC1 Project</ItemName><IECProjectDef>" + projectInfo + "<OnlineSettings /></IECProjectDef></TreeItem>";
    static string Info(string company, string title, string version) =>
        Project("<ProjectInfo>" + company + title + version + "<Author /></ProjectInfo>");
    const string C = "<Company>Qualification</Company>", T = "<Title>QualLib</Title>", V = "<Version>1.0.0</Version>";

    public static void Run()
    {
        Check(Refusal(Info(C, T, V)) == null, "A project with Company, Title and Version passes");
        Check(Refusal(Info("<Company> ACME </Company>", "<Title> Lib </Title>", "<Version>2.1.0.4</Version>")) == null, "Values with spaces around them pass");

        // Measured: XAE refuses each missing field, and the refusal used to read as an unknown outcome.
        Check(Refusal(Info("", T, V))?.EndsWith("Not set: Company. Set them in the PLC project properties before a library export.") == true, "A missing Company refuses and is named");
        Check(Refusal(Info(C, "", V))?.Contains("Not set: Title.") == true, "A missing Title refuses and is named");
        Check(Refusal(Info(C, T, ""))?.Contains("Not set: Version.") == true, "A missing Version refuses and is named");
        Check(Refusal(Info("", T, ""))?.Contains("Not set: Company, Version.") == true, "Every missing field is named");
        Check(Refusal(Info("<Company />", "<Title />", "<Version />"))?.Contains("Not set: Company, Title, Version.") == true, "Empty elements refuse");
        Check(Refusal(Info(C, "<Title>   </Title>", V)) != null, "A blank Title refuses");
        Check(Refusal(Info(C, "<Title>&#160;&#160;</Title>", V)) != null, "A Title of non-breaking spaces refuses");
        Check(Refusal(Info(C, T, "<ProjectVersion>1.0.0</ProjectVersion>"))?.Contains("Not set: Version.") == true, "The .plcproj name ProjectVersion is not the XML Version");
        Check(Refusal(Project(""))?.Contains("Not set: Company, Title, Version.") == true, "A missing ProjectInfo refuses");
        Check(Refusal(Project("<ProjectInfo>" + C + V + "</ProjectInfo>" + T)) != null, "A Title outside ProjectInfo does not count");
        Check(Refusal("<TreeItem><ProjectInfo>" + C + T + V + "</ProjectInfo></TreeItem>") != null, "ProjectInfo outside IECProjectDef does not count");
        Console.WriteLine("PASS: " + cases + " library project info pre-check cases; no XAE or PLC used.");
    }
}
