using System;
using System.Text.Json;
using System.Xml;
using TcAutomation.Core;
using TCatSysManagerLib;

namespace TcAutomation.Commands
{
    /// <summary>
    /// Configures a real-time task (enable/disable, autostart).
    /// Uses native TwinCAT Automation Interface API where possible:
    /// - ITcSmTreeItem.Disabled for enable/disable (native API)
    /// - XML for AutoStart (no native API available)
    /// </summary>
    public static class ConfigureTaskCommand
    {
        private const string REAL_TIME_TASKS_SHORTCUT = "TIRT";

        public static int Execute(string solutionPath, string taskName, bool? enable, bool? autoStart, string? tcVersion)
        {
            VisualStudioInstance? vsInstance = null;
            var result = new ConfigureTaskResult();

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

                result = ExecuteInSession(vsInstance, solutionPath, taskName, enable, autoStart);
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return result.Success ? 0 : 1;
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
        /// Configure a real-time task using an already-open VS instance. Used by batch mode.
        /// </summary>
        public static ConfigureTaskResult ExecuteInSession(VisualStudioInstance vsInstance, string solutionPath, string taskName, bool? enable, bool? autoStart)
        {
            var result = new ConfigureTaskResult
            {
                SolutionPath = solutionPath,
                TaskName = taskName
            };

            bool written = false;
            try
            {
                var automation = new AutomationInterface(vsInstance);

                ITcSmTreeItem tasksTreeItem;
                try
                {
                    tasksTreeItem = automation.SystemManager.LookupTreeItem(REAL_TIME_TASKS_SHORTCUT);
                }
                catch
                {
                    result.ErrorMessage = "Real-time tasks tree not found in project";
                    return result;
                }

                ITcSmTreeItem? targetTask = null;
                for (int i = 1; i <= tasksTreeItem.ChildCount; i++)
                {
                    var taskItem = tasksTreeItem.Child[i];
                    string xml = taskItem.ProduceXml();
                    string itemName = GetItemNameFromXml(xml);

                    if (taskItem.Name.Equals(taskName, StringComparison.OrdinalIgnoreCase) ||
                        itemName.Equals(taskName, StringComparison.OrdinalIgnoreCase))
                    {
                        targetTask = taskItem;
                        break;
                    }
                }

                if (targetTask == null)
                {
                    result.ErrorMessage = $"Task '{taskName}' not found. Available tasks: ";
                    for (int i = 1; i <= tasksTreeItem.ChildCount; i++)
                    {
                        result.ErrorMessage += tasksTreeItem.Child[i].Name;
                        if (i < tasksTreeItem.ChildCount) result.ErrorMessage += ", ";
                    }
                    return result;
                }

                string currentXml = targetTask.ProduceXml();

                bool wasDisabled = (targetTask.Disabled != DISABLED_STATE.SMDS_NOT_DISABLED);
                bool? wasAutoStart = GetAutoStartFromXml(currentXml);
                result.PreviousDisabled = wasDisabled;
                result.PreviousAutoStart = wasAutoStart;
                // Until read back, the observed state is the old one.
                result.NewDisabled = wasDisabled;
                result.NewAutoStart = wasAutoStart;

                if (autoStart.HasValue && wasAutoStart == null)
                {
                    result.ErrorMessage = $"Task '{taskName}' has no TaskDef/AutoStart node; nothing changed.";
                    return result;
                }

                bool newDisabled = enable.HasValue ? !enable.Value : wasDisabled;
                if (autoStart.HasValue && autoStart != wasAutoStart)
                {
                    // Before Disabled: this XML also carries the old Disabled state.
                    written = true;
                    targetTask.ConsumeXml(SetAutoStartInXml(currentXml, autoStart.Value));
                }

                // Only when asked: writing it would also replace an inherited SMDS_PARENT_DISABLED.
                if (enable.HasValue)
                {
                    written = true;
                    targetTask.Disabled = newDisabled ? DISABLED_STATE.SMDS_DISABLED : DISABLED_STATE.SMDS_NOT_DISABLED;
                }

                System.Threading.Thread.Sleep(1000);

                // Report what the project holds, not what was requested.
                result.NewDisabled = targetTask.Disabled != DISABLED_STATE.SMDS_NOT_DISABLED;
                result.NewAutoStart = GetAutoStartFromXml(targetTask.ProduceXml());
                bool? newAutoStart = autoStart ?? wasAutoStart;
                if (result.NewDisabled != newDisabled || (autoStart.HasValue && result.NewAutoStart != newAutoStart))
                {
                    result.ErrorMessage = $"Task '{taskName}' reads back Disabled={result.NewDisabled}, AutoStart={Show(result.NewAutoStart)}; " +
                                          $"requested Disabled={newDisabled}, AutoStart={Show(newAutoStart)}.";
                    return result;
                }
                result.Success = true;
                result.Message = $"Task '{taskName}' configured: Disabled={newDisabled}, AutoStart={Show(newAutoStart)}";
            }
            catch (Exception ex)
            {
                result.ErrorMessage = ex.Message;
                if (written)
                {
                    // A write may have landed before the fault; the new state was not read back.
                    result.OutcomeUnknown = true;
                    result.NewAutoStart = null;
                }
            }

            return result;
        }

        private static string Show(bool? value) => value?.ToString() ?? "unknown";

        private static string GetItemNameFromXml(string xml)
        {
            var xmlDoc = new XmlDocument();
            xmlDoc.LoadXml(xml);
            var itemNameNode = xmlDoc.SelectSingleNode("/TreeItem/ItemName");
            return itemNameNode?.InnerText ?? "";
        }

        // Null when the node is absent: unknown, not false.
        private static bool? GetAutoStartFromXml(string xml)
        {
            var xmlDoc = new XmlDocument();
            xmlDoc.LoadXml(xml);

            var autoStartNode = xmlDoc.SelectSingleNode("/TreeItem/TaskDef/AutoStart");
            if (autoStartNode != null)
            {
                return autoStartNode.InnerText.Equals("true", StringComparison.OrdinalIgnoreCase);
            }
            return null;
        }

        private static string SetAutoStartInXml(string xml, bool autoStart)
        {
            var xmlDoc = new XmlDocument();
            xmlDoc.LoadXml(xml);

            var autoStartNode = xmlDoc.SelectSingleNode("/TreeItem/TaskDef/AutoStart");
            if (autoStartNode != null)
            {
                autoStartNode.InnerText = autoStart.ToString().ToLower();
                return xmlDoc.OuterXml;
            }

            return "";
        }
    }

    public class ConfigureTaskResult
    {
        public string SolutionPath { get; set; } = "";
        public string TaskName { get; set; } = "";
        public bool Success { get; set; }
        public bool OutcomeUnknown { get; set; }
        public string? Message { get; set; }
        public bool PreviousDisabled { get; set; }
        public bool? PreviousAutoStart { get; set; }
        public bool NewDisabled { get; set; }
        public bool? NewAutoStart { get; set; }
        public string? ErrorMessage { get; set; }
    }
}
