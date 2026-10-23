using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("1.0.0.0")]
[assembly: System.Reflection.AssemblyInformationalVersion("1.0")]
[assembly: System.Reflection.AssemblyProduct("MYN")]
class Launcher {
    [STAThread] static int Main() {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "runtime", "main", "pythonw.exe");
        string script = Path.Combine(root, "src", "myn_gui.py");
        if (!File.Exists(python) || !File.Exists(script) || !File.Exists(Path.Combine(root, "installation-ready.json"))) {
            MessageBox.Show("MYN files are missing. Run the MYN installer again to repair them.", "MYN");
            return 1;
        }
        try {
            var start = new ProcessStartInfo(python, "-B \"" + script + "\"");
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WorkingDirectory = root;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            Process.Start(start);
            return 0;
        } catch (Exception error) {
            MessageBox.Show("MYN could not start: " + error.Message, "MYN");
            return 1;
        }
    }
}
