using System;
using System.Collections.Generic;
using System.Linq;
using System.Xml;
using TCatSysManagerLib;

namespace TcAutomation.Core
{
    /// <summary>One real-time task, as the TcUnit run configures it.</summary>
    public interface ITaskNode
    {
        string ProduceXml();
        void ConsumeXml(string xml);
        bool Disabled { get; set; }
        /// <summary>The native disabled state, so a restore keeps an inherited (parent) state exactly.</summary>
        int NativeState { get; set; }
    }

    public sealed class TreeTaskNode : ITaskNode
    {
        private readonly ITcSmTreeItem _item;
        public TreeTaskNode(ITcSmTreeItem item) { _item = item; }
        public string ProduceXml() => _item.ProduceXml();
        public void ConsumeXml(string xml) => _item.ConsumeXml(xml);
        public bool Disabled
        {
            get => _item.Disabled != DISABLED_STATE.SMDS_NOT_DISABLED;
            set => _item.Disabled = value ? DISABLED_STATE.SMDS_DISABLED : DISABLED_STATE.SMDS_NOT_DISABLED;
        }
        public int NativeState
        {
            get => (int)_item.Disabled;
            set => _item.Disabled = (DISABLED_STATE)value;
        }
    }

    public sealed class TaskConfiguration
    {
        public int ChangedTasks { get; internal set; }
        public Action Restore { get; internal set; } = () => { };
    }

    /// <summary>
    /// Enables and autostarts the test task and, on request, disables and stops autostart of the others.
    /// The task XML has no TaskDef/Disabled node, so Disabled is set through the native property.
    /// </summary>
    public static class TcUnitTasks
    {
        const string AutoStartPath = "/TreeItem/TaskDef/AutoStart";

        /// <summary>
        /// The end of the whole TcUnit run. The server stops waiting timeoutMinutes plus 180 s after
        /// dispatch (dispatch.py, cli.py), so the run ends 30 s earlier and its result still arrives.
        /// </summary>
        public static DateTime RunDeadline(DateTime startUtc, int timeoutMinutes) =>
            startUtc.AddMinutes(timeoutMinutes).AddSeconds(150);

        /// <summary>
        /// The last moment to start a wait or poll iteration. One iteration takes up to 20 s (a 5 s poll and
        /// a 10 s settle), and the restore takes about 2 s per changed task; both fit before RunDeadline.
        /// </summary>
        public static DateTime PollDeadline(DateTime startUtc, int timeoutMinutes, int changedTasks) =>
            RunDeadline(startUtc, timeoutMinutes).AddSeconds(-20 - 2 * changedTasks);

        /// <summary>
        /// Configures the tasks. Nothing is written unless the test task exists and every task to change has
        /// an AutoStart node. The returned Restore puts each changed task back to its original state.
        /// </summary>
        public static TaskConfiguration Configure(IReadOnlyList<ITaskNode> tasks, string taskName, bool disableOthers, Action pause, Action restorePause)
        {
            var plan = new List<(ITaskNode Task, string Name, bool Target, bool WasAutoStart, int WasState)>();
            foreach (var task in tasks)
            {
                string xml = task.ProduceXml();
                string name = Name(xml);
                bool target = name.Equals(taskName, StringComparison.OrdinalIgnoreCase);
                if (!target && !disableOthers) continue;
                plan.Add((task, name, target, AutoStart(xml, name), task.NativeState));
            }
            if (!plan.Any(p => p.Target))
                throw new InvalidOperationException($"Task '{taskName}' not found.");

            var changed = new List<(ITaskNode Task, string Name, bool AutoStart, int State)>();
            var result = new TaskConfiguration();
            result.Restore = () =>
            {
                var errors = new List<string>();
                foreach (var c in Enumerable.Reverse(changed))
                {
                    try { Restore(c.Task, c.Name, c.AutoStart, c.State, restorePause); }
                    catch (Exception ex) { errors.Add(ex.Message); }
                }
                if (errors.Count > 0)
                    throw new InvalidOperationException("Task states not restored: " + string.Join(" ", errors));
            };
            try
            {
                foreach (var p in plan)
                {
                    changed.Add((p.Task, p.Name, p.WasAutoStart, p.WasState));  // before the write: a failed write is restored too
                    Apply(p.Task, p.Name, p.Target, !p.Target, pause);
                }
            }
            catch (Exception configure)
            {
                try { result.Restore(); }
                catch (Exception restore) { throw new InvalidOperationException(configure.Message + " " + restore.Message, configure); }
                throw;
            }
            result.ChangedTasks = changed.Count;
            return result;
        }

        static XmlDocument Load(ITaskNode task)
        {
            var doc = new XmlDocument();
            doc.LoadXml(task.ProduceXml());
            return doc;
        }

        // AutoStart through the XML first: consuming it restores the Disabled state it carries.
        static void WriteAutoStart(ITaskNode task, string name, bool autoStart)
        {
            var doc = Load(task);
            var node = doc.SelectSingleNode(AutoStartPath)
                ?? throw new InvalidOperationException($"Task '{name}' has no TaskDef/AutoStart node.");
            string want = autoStart ? "true" : "false";
            if (node.InnerText.Equals(want, StringComparison.OrdinalIgnoreCase)) return;
            node.InnerText = want;
            task.ConsumeXml(doc.OuterXml);
        }

        static bool ReadAutoStart(ITaskNode task) =>
            string.Equals(Load(task).SelectSingleNode(AutoStartPath)?.InnerText, "true", StringComparison.OrdinalIgnoreCase);

        static void Apply(ITaskNode task, string name, bool autoStart, bool disabled, Action pause)
        {
            WriteAutoStart(task, name, autoStart);
            task.Disabled = disabled;
            pause();  // before the read-back, as ConfigureTaskCommand waits before its own
            if (task.Disabled != disabled || ReadAutoStart(task) != autoStart)
                throw new InvalidOperationException(
                    $"Task '{name}' reads back Disabled={task.Disabled}, AutoStart={ReadAutoStart(task)}; requested Disabled={disabled}, AutoStart={autoStart}.");
        }

        static void Restore(ITaskNode task, string name, bool autoStart, int state, Action pause)
        {
            WriteAutoStart(task, name, autoStart);
            task.NativeState = state;
            pause();
            if (task.NativeState != state || ReadAutoStart(task) != autoStart)
                throw new InvalidOperationException(
                    $"Task '{name}' reads back state {task.NativeState}, AutoStart={ReadAutoStart(task)}; original state {state}, AutoStart={autoStart}.");
        }

        static bool AutoStart(string xml, string name)
        {
            var doc = new XmlDocument();
            doc.LoadXml(xml);
            var node = doc.SelectSingleNode(AutoStartPath)
                ?? throw new InvalidOperationException($"Task '{name}' has no TaskDef/AutoStart node.");
            return node.InnerText.Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        static string Name(string xml)
        {
            var doc = new XmlDocument();
            doc.LoadXml(xml);
            return doc.SelectSingleNode("/TreeItem/ItemName")?.InnerText ?? "";
        }
    }
}
