using System;
using System.Text.Json;
using TcAutomation.Core;

namespace TcAutomation.Commands
{
    /// <summary>
    /// Lists all PLC projects in a TwinCAT solution.
    /// Returns detailed info about each PLC project including name, AMS port, and autostart status.
    /// </summary>
    public static class ListPlcsCommand
    {
        public static int Execute(string solutionPath, string? tcVersion)
        {
            VisualStudioInstance? vsInstance = null;
            var result = new ListPlcsResult();

            try
            {
                // Find TwinCAT project and version
                string tsprojPath = TcFileUtilities.FindTwinCATProjectFile(solutionPath);
                if (string.IsNullOrEmpty(tsprojPath))
                {
                    result.ErrorMessage = "Could not find TwinCAT project file (.tsproj) in solution";
                    Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                    return 1;
                }

                string projectTcVersion = TcFileUtilities.GetTcVersion(tsprojPath);

                // Load Visual Studio
                vsInstance = new VisualStudioInstance(solutionPath, projectTcVersion, tcVersion);
                vsInstance.Load();
                vsInstance.LoadSolution();

                result = ExecuteInSession(vsInstance, solutionPath, string.IsNullOrEmpty(tcVersion) ? projectTcVersion : tcVersion);
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return string.IsNullOrEmpty(result.ErrorMessage) ? 0 : 1;
            }
            catch (Exception ex)
            {
                result.ErrorMessage = ex.Message;
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 1;
            }
            finally
            {
                vsInstance?.Close();
            }
        }

        /// <summary>
        /// List PLC projects using an already-open VS instance. Used by batch mode.
        /// </summary>
        public static ListPlcsResult ExecuteInSession(VisualStudioInstance vsInstance, string solutionPath, string tcVersion)
        {
            var result = new ListPlcsResult
            {
                SolutionPath = solutionPath,
                TcVersion = tcVersion
            };

            try
            {
                var automation = new AutomationInterface(vsInstance);
                result.PlcCount = automation.PlcTreeItem.ChildCount;

                for (int i = 1; i <= automation.PlcTreeItem.ChildCount; i++)
                {
                    var plcProject = automation.PlcTreeItem.Child[i];
                    var plcInfo = new ListPlcInfo
                    {
                        Name = plcProject.Name,
                        Index = i
                    };

                    try
                    {
                        var iecProject = (TCatSysManagerLib.ITcPlcProject)plcProject;
                        plcInfo.BootProjectAutostart = iecProject.BootProjectAutostart;

                        plcInfo.AmsPort = AutomationInterface.ConfiguredAdsPort(plcProject.ProduceXml(false));
                    }
                    catch (Exception ex)
                    {
                        plcInfo.Error = ex.Message;
                    }

                    result.PlcProjects.Add(plcInfo);
                }
            }
            catch (Exception ex)
            {
                result.ErrorMessage = ex.Message;
            }

            return result;
        }
    }

    public class ListPlcsResult
    {
        public string SolutionPath { get; set; } = "";
        public string TcVersion { get; set; } = "";
        public int PlcCount { get; set; }
        public System.Collections.Generic.List<ListPlcInfo> PlcProjects { get; set; } = new System.Collections.Generic.List<ListPlcInfo>();
        public string? ErrorMessage { get; set; }
    }

    public class ListPlcInfo
    {
        public string Name { get; set; } = "";
        public int Index { get; set; }
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.Never)]
        public int? AmsPort { get; set; }  // null when unknown, also under WhenWritingNull
        public bool BootProjectAutostart { get; set; }
        public string? Error { get; set; }
    }
}
