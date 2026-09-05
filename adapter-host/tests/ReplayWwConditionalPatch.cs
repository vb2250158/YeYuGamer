using System;
using System.IO;
using System.Reflection;
using System.Text;

internal static class ReplayWwConditionalPatch
{
    public static int Main(string[] args)
    {
        if (args.Length != 3) return 64;
        Assembly runner = Assembly.LoadFrom(Path.GetFullPath(args[0]));
        Type program = runner.GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        MethodInfo patch = program.GetMethod("PatchWwConditionalStageBoundaries", BindingFlags.NonPublic | BindingFlags.Static);
        if (patch == null) throw new InvalidOperationException("compiled WW patch is missing");
        string original = File.ReadAllText(args[1], Encoding.UTF8);
        string updated = (string)patch.Invoke(null, new object[] { original });
        string repeated = (string)patch.Invoke(null, new object[] { updated });
        if (updated != repeated) throw new InvalidOperationException("WW patch is not idempotent");
        try {
            patch.Invoke(null, new object[] { original.Replace("manager_requires_nightmare = (", "changed_upstream_condition = (") });
            throw new InvalidOperationException("changed upstream source was accepted");
        } catch (TargetInvocationException error) {
            if (!(error.InnerException is InvalidOperationException)) throw;
        }
        File.WriteAllText(args[2], updated, new UTF8Encoding(false));
        return 0;
    }
}
