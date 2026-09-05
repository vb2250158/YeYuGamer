using System;
using System.Threading;

internal static class FakeStarRailTool
{
    public static int Main()
    {
        Thread.Sleep(TimeSpan.FromSeconds(90));
        return 0;
    }
}
