using System;
using System.Reflection;

// Invoke the production selection mapping without launching a tool or game.
internal static class EndfieldConfigReplay
{
    static int Main(string[] args)
    {
        Type program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        Type type = program.GetNestedType("Binding", BindingFlags.NonPublic);
        object binding = Activator.CreateInstance(type, true);
        type.GetField("GameId").SetValue(binding, "Endfield");
        type.GetField("DailyConfig").SetValue(binding, args[1]);
        string[] selected = args[2].Length == 0 ? new string[0] : args[2].Split(',');
        program.GetMethod("ApplySelectedOfficialDailyConfig", BindingFlags.Static | BindingFlags.NonPublic)
            .Invoke(null, new object[] { binding, selected });
        return 0;
    }
}
