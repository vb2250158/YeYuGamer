using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;

internal static class ConfigAtomicReplaceReplay {
    static int Main(string[] args) {
        string root = args[1];
        Directory.CreateDirectory(root);
        string path = Path.Combine(root, "scope.json");
        byte[] original = Encoding.UTF8.GetBytes("{\"privateSetting\":\"preserve\"}");
        MethodInfo write = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true)
            .GetMethod("WriteConfigDocument", BindingFlags.Static | BindingFlags.NonPublic);
        try {
            File.WriteAllBytes(path, original);
            FileStream locked = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            Thread release = new Thread(delegate() { Thread.Sleep(180); locked.Dispose(); });
            release.Start();
            try { write.Invoke(null, new object[] { path, new Dictionary<string, object> { { "scope", "selected" } } }); }
            finally { release.Join(); locked.Dispose(); }
            if (!File.ReadAllText(path).Contains("selected")) throw new Exception("Temporary lock did not recover.");

            File.WriteAllBytes(path, original);
            bool rejected = false;
            using (FileStream persistent = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.Read)) {
                try { write.Invoke(null, new object[] { path, new Dictionary<string, object> { { "scope", "must-not-write" } } }); }
                catch (TargetInvocationException error) {
                    if (!(error.InnerException is IOException)) throw;
                    rejected = true;
                }
                if (!rejected || Convert.ToBase64String(File.ReadAllBytes(path)) != Convert.ToBase64String(original))
                    throw new Exception("Persistent lock must fail without changing destination bytes.");
            }
            if (Directory.GetFiles(root, "*.tmp").Length != 0) throw new Exception("Temporary configuration leaked.");
            Console.WriteLine("{\"suite\":\"atomic-config-replacement\",\"passed\":true,\"transientLockRecovered\":true,\"persistentLockPreservedOriginal\":true,\"gameStarted\":false}");
            return 0;
        } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
