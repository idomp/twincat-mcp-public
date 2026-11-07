using System;
using System.Collections.Generic;
using TcAutomation.Core;

static class TcUnitTaskTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }
    static bool Throws(Action action)
    {
        try { action(); return false; } catch (InvalidOperationException) { return true; }
    }

    // Disabled lives outside TaskDef. ConsumeXml applies the Disabled state the XML carries, as XAE did (X03).
    // With Lag, a Disabled write takes effect only after the next pause.
    sealed class FakeTask : ITaskNode
    {
        public string Name;
        public bool AutoStart, IgnoreDisabled, Lag;
        public int Writes;
        bool disabled;
        bool? pending;
        public FakeTask(string name, bool disabled, bool autoStart) { Name = name; this.disabled = disabled; AutoStart = autoStart; }
        public int Parent;  // models SMDS_PARENT_DISABLED as native state 2
        public bool Disabled
        {
            get => disabled || Parent == 2;
            set { Writes++; Parent = 0; if (IgnoreDisabled) return; if (Lag) pending = value; else disabled = value; }
        }
        public int NativeState
        {
            get => Parent == 2 ? 2 : disabled ? 1 : 0;
            set { Writes++; Parent = value == 2 ? 2 : 0; disabled = value == 1; }
        }
        public void Settle() { if (pending.HasValue) { disabled = pending.Value; pending = null; } }
        public string ProduceXml() =>
            $"<TreeItem><ItemName>{Name}</ItemName><Disabled>{(disabled ? "true" : "false")}</Disabled>" +
            $"<TaskDef><AutoStart>{(AutoStart ? "true" : "false")}</AutoStart></TaskDef></TreeItem>";
        public void ConsumeXml(string xml)
        {
            Writes++;
            AutoStart = xml.Contains("<AutoStart>true</AutoStart>");
            disabled = xml.Contains("<Disabled>true</Disabled>");
        }
    }

    public static void Run()
    {
        var test = new FakeTask("TestTask", true, false);
        var plc = new FakeTask("PlcTask", false, true);
        var io = new FakeTask("IoTask", false, false);
        int pauses = 0;
        var restore = TcUnitTasks.Configure(new List<ITaskNode> { plc, test, io }, "testtask", true, () => pauses++, () => { }).Restore;
        Check(!test.Disabled && test.AutoStart, "Test task enabled and autostarted");
        Check(plc.Disabled && !plc.AutoStart, "Other task disabled, autostart off");
        Check(io.Disabled && !io.AutoStart, "Task already without autostart still disabled");
        Check(pauses == 3, "One pause per configured task");
        restore();
        Check(test.Disabled && !test.AutoStart && !plc.Disabled && plc.AutoStart && !io.Disabled && !io.AutoStart,
            "Restore returns every task to its original state");

        test = new FakeTask("TestTask", true, true);
        plc = new FakeTask("PlcTask", false, true);
        TcUnitTasks.Configure(new List<ITaskNode> { test, plc }, "TestTask", false, () => { }, () => { });
        Check(!test.Disabled && test.AutoStart, "Disabled test task enabled with autostart already on");
        Check(!plc.Disabled && plc.AutoStart && plc.Writes == 0, "Other tasks untouched without disableOthers");

        plc = new FakeTask("PlcTask", false, true);
        Check(Throws(() => TcUnitTasks.Configure(new List<ITaskNode> { plc }, "TestTask", true, () => { }, () => { })),
            "Missing test task fails");
        Check(plc.Writes == 0 && !plc.Disabled, "A missing test task fails before any write");

        var stuck = new FakeTask("PlcTask", false, true) { IgnoreDisabled = true };
        var first = new FakeTask("TestTask", true, false);
        Check(Throws(() => TcUnitTasks.Configure(new List<ITaskNode> { first, stuck }, "TestTask", true, () => { }, () => { })),
            "A task that reads back enabled fails");
        Check(first.Disabled && !first.AutoStart && stuck.AutoStart, "A failed configuration restores the tasks it changed");

        var inherited = new FakeTask("IoTask", false, true) { Parent = 2 };
        var tt = new FakeTask("TestTask", false, true);
        var cfg = TcUnitTasks.Configure(new List<ITaskNode> { tt, inherited }, "TestTask", true, () => { }, () => { });
        Check(cfg.ChangedTasks == 2, "Changed tasks are counted for the restore reserve");
        cfg.Restore();
        Check(inherited.NativeState == 2 && inherited.AutoStart, "Restore keeps an inherited disabled state exactly");

        var lagging = new FakeTask("PlcTask", false, true) { Lag = true };
        var target = new FakeTask("TestTask", false, true);
        TcUnitTasks.Configure(new List<ITaskNode> { target, lagging }, "TestTask", true, () => lagging.Settle(), () => { });
        Check(lagging.Disabled, "The read-back follows the pause, so a lagging write still verifies");

        var start = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        var deadline = TcUnitTasks.RunDeadline(start, 10);
        Check(deadline > start.AddMinutes(10) && deadline < start.AddMinutes(10).AddSeconds(180),
            "TcUnit deadline ends after timeoutMinutes and before the server stops waiting");
        Check(TcUnitTasks.PollDeadline(start, 10, 5).AddSeconds(20 + 2 * 5) == deadline, "The poll deadline reserves the last poll and the restore");
        Console.WriteLine("PASS: " + cases + " TcUnit task configuration and deadline cases; no XAE or PLC used.");
    }
}
