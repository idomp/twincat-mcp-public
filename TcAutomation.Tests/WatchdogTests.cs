using System;
using System.Diagnostics;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;
using TcAutomation.Core;

static class WatchdogTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }
    static bool Eventually(Func<bool> condition, int ms = 3000)
    {
        var end = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < end) { if (condition()) return true; Thread.Sleep(20); }
        return condition();
    }

    // Counts BM_CLICK. A hidden form does not raise Click, but the message is what the watchdog sends.
    sealed class CountingButton : Button
    {
        public int Clicks;
        protected override void WndProc(ref Message m) { if (m.Msg == 0x00F5) Clicks++; base.WndProc(ref m); }
    }

    static bool Click(IntPtr dialog, string text) =>
        (bool)typeof(DialogWatchdog).GetMethod("ClickButton", BindingFlags.NonPublic | BindingFlags.Static).Invoke(null, new object[] { dialog, text });

    static void ClickRecheck(int self)
    {
        using (var form = new Form { ShowInTaskbar = false })
        {
            var ok = new CountingButton { Text = "OK" };
            form.Controls.Add(ok);
            IntPtr dialog = form.Handle;
            GC.KeepAlive(ok.Handle);
            Check(!Click(dialog, "OK") && ok.Clicks == 0, "An unregistered shell gets no click");
            DialogWatchdog.Start(self);
            try { bool sent = Click(dialog, "OK"); Check(sent && ok.Clicks == 1, $"A registered shell gets the click (sent {sent}, clicks {ok.Clicks})"); }
            finally { DialogWatchdog.Stop(self); }
            Check(!Click(dialog, "OK") && ok.Clicks == 1, "A shell deregistered by Stop gets no click");
            Check(!Click(dialog, "Cancel") && ok.Clicks == 1, "A missing button gets no click");
        }
    }

    public static void Run()
    {
        // A Stop whose Join times out, then a Start: one watchdog thread, not two.
        int self = Process.GetCurrentProcess().Id;
        DialogWatchdog.StopJoinMs = 0;
        try
        {
            DialogWatchdog.Start(self);
            Check(Eventually(() => DialogWatchdog.LiveWorkers == 1), "Watchdog thread started");
            DialogWatchdog.Stop(self);
            DialogWatchdog.Start(self);
            Thread.Sleep(600);
            Check(DialogWatchdog.LiveWorkers == 1, $"One watchdog thread after a restart, found {DialogWatchdog.LiveWorkers}");
            DialogWatchdog.Stop(self);
            Check(Eventually(() => DialogWatchdog.LiveWorkers == 0), "Watchdog thread exits after the last Stop");
            DialogWatchdog.Start(self);
            Check(Eventually(() => DialogWatchdog.LiveWorkers == 1), "Watchdog restarts after a clean exit");
            DialogWatchdog.Stop(self);
            Check(Eventually(() => DialogWatchdog.LiveWorkers == 0), "Watchdog thread exits again");
        }
        finally { DialogWatchdog.StopJoinMs = 1000; }

        // A click goes only to a shell that is still registered. The window is a hidden form of this process.
        Exception failure = null;
        var ui = new Thread(() => { try { ClickRecheck(self); } catch (Exception ex) { failure = ex; } });
        ui.SetApartmentState(ApartmentState.STA);
        ui.Start();
        ui.Join();
        if (failure != null) throw failure;
        Console.WriteLine("PASS: " + cases + " dialog watchdog thread cases; no XAE or PLC used.");
    }
}
