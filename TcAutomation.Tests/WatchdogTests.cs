using System;
using System.Diagnostics;
using System.Threading;
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
        Console.WriteLine("PASS: " + cases + " dialog watchdog thread cases; no XAE or PLC used.");
    }
}
