// OmniBots Setup: the Windows installer (PLAN.md A17.99).
//
// One .exe that carries everything it needs: install.ps1 (the same script as the one-line install), the OmniBots code
// as a git bundle (so it installs exactly the version printed on it, even offline from GitHub) and Omi's icon. It
// shows what install.ps1 does, live, and offers to start OmniBots at the end. Later updates come from GitHub as usual.
//
// Built by installer/build.py with the C# compiler that ships with Windows (.NET Framework 4.x), so it runs on any
// Windows 10/11 without installing anything first. C# 5: no string interpolation, no "=>" members.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("OmniBots Setup")]
[assembly: AssemblyProduct("OmniBots")]
[assembly: AssemblyCompany("Global Warning Networks")]
[assembly: AssemblyCopyright("(c) 2026 omnibots.globalwarningnetworks.com")]
[assembly: AssemblyVersion(OmniBotsSetup.Info.InstallerVersion + ".0.0")]
[assembly: AssemblyFileVersion(OmniBotsSetup.Info.InstallerVersion + ".0.0")]
[assembly: AssemblyInformationalVersion("Installer " + OmniBotsSetup.Info.InstallerVersion + " for OmniBots " + OmniBotsSetup.Info.AppVersion)]

namespace OmniBotsSetup
{
    public static partial class Info
    {
        // InstallerVersion and AppVersion come from Version.cs, written by build.py
    }

    public class SetupForm : Form
    {
        static readonly Color Navy = Color.FromArgb(7, 11, 24);
        static readonly Color PanelBg = Color.FromArgb(17, 26, 53);
        static readonly Color Border = Color.FromArgb(34, 52, 95);
        static readonly Color Cyan = Color.FromArgb(111, 227, 255);
        static readonly Color Accent = Color.FromArgb(47, 125, 255);
        static readonly Color Dim = Color.FromArgb(139, 151, 184);
        static readonly Color Text1 = Color.FromArgb(231, 237, 251);

        TextBox folder;
        CheckBox shortcut, browser, winget;
        Button install, browse, launch, close;
        RichTextBox log;
        Label status;
        Process proc;
        string work;
        string installedDir;

        public SetupForm()
        {
            Text = "OmniBots " + Info.AppVersion + " Setup";
            BackColor = Navy;
            ForeColor = Text1;
            Font = new Font("Segoe UI", 10f);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(760, 600);
            try { Icon = new Icon(Resource("omi.ico")); } catch { }

            var logo = new PictureBox { Location = new Point(28, 22), Size = new Size(64, 64), SizeMode = PictureBoxSizeMode.Zoom };
            try { logo.Image = new Icon(Resource("omi.ico"), 64, 64).ToBitmap(); } catch { }
            Controls.Add(logo);
            Controls.Add(new Label { Text = "OmniBots", Location = new Point(104, 22), AutoSize = true,
                                     Font = new Font("Segoe UI Semibold", 22f), ForeColor = Text1 });
            Controls.Add(new Label { Text = "Version " + Info.AppVersion + "  ·  Installer v" + Info.InstallerVersion,
                                     Location = new Point(108, 64), AutoSize = true, ForeColor = Cyan });
            Controls.Add(new Label { Text = "One team of AI bots on your desktop. Omi, the boss bot, plans your goal, builds a team, "
                                          + "checks their work and asks you before anything risky.",
                                     Location = new Point(30, 98), Size = new Size(700, 44), ForeColor = Dim });

            Controls.Add(new Label { Text = "Install into", Location = new Point(30, 150), AutoSize = true });
            folder = new TextBox { Location = new Point(30, 174), Size = new Size(590, 28), BackColor = PanelBg, ForeColor = Text1,
                                   BorderStyle = BorderStyle.FixedSingle,
                                   Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".omnibots") };
            Controls.Add(folder);
            browse = MakeButton("Browse…", new Point(630, 172), new Size(100, 30), false);
            browse.Click += delegate {
                var d = new FolderBrowserDialog { Description = "Where OmniBots goes (your bots' data stays next to it)", SelectedPath = folder.Text };
                if (d.ShowDialog(this) == DialogResult.OK) folder.Text = d.SelectedPath;
            };
            Controls.Add(browse);
            Controls.Add(new Label { Text = "Already installed there? It's updated in place: your bots, projects and settings stay.",
                                     Location = new Point(30, 206), AutoSize = true, ForeColor = Dim, Font = new Font("Segoe UI", 9f) });

            shortcut = MakeCheck("Add OmniBots to the Start menu", new Point(30, 236), true);
            browser = MakeCheck("Install the bots' web browser (Chromium, about 150 MB)", new Point(30, 262), true);
            winget = MakeCheck("If Python 3.12+ or Git is missing, install it with winget", new Point(30, 288), true);

            log = new RichTextBox { Location = new Point(30, 322), Size = new Size(700, 196), ReadOnly = true, BackColor = PanelBg,
                                    ForeColor = Text1, BorderStyle = BorderStyle.None, Font = new Font("Cascadia Mono", 9f),
                                    Text = "Ready. Nothing is changed until you press Install." + Environment.NewLine };
            Controls.Add(log);
            status = new Label { Location = new Point(30, 530), Size = new Size(420, 40), ForeColor = Dim };
            Controls.Add(status);

            close = MakeButton("Close", new Point(470, 534), new Size(80, 36), false);
            close.Click += delegate { Close(); };
            Controls.Add(close);
            launch = MakeButton("Start OmniBots", new Point(560, 534), new Size(170, 36), true);
            launch.Visible = false;
            launch.Click += delegate { Launch(); };
            Controls.Add(launch);
            install = MakeButton("Install", new Point(560, 534), new Size(170, 36), true);
            install.Click += delegate { Run(); };
            Controls.Add(install);
            FormClosing += delegate(object s, FormClosingEventArgs e) {
                if (proc != null && !proc.HasExited) {
                    if (MessageBox.Show(this, "The install is still running. Stop it?", "OmniBots Setup", MessageBoxButtons.YesNo) != DialogResult.Yes) {
                        e.Cancel = true;
                        return;
                    }
                    try { proc.Kill(); } catch { }
                }
                Cleanup();
            };
        }

        CheckBox MakeCheck(string text, Point at, bool on)
        {
            var c = new CheckBox { Text = text, Location = at, AutoSize = true, Checked = on, ForeColor = Text1 };
            Controls.Add(c);
            return c;
        }

        Button MakeButton(string text, Point at, Size size, bool primary)
        {
            var b = new Button { Text = text, Location = at, Size = size, FlatStyle = FlatStyle.Flat, ForeColor = Text1,
                                 BackColor = primary ? Accent : PanelBg, Cursor = Cursors.Hand };
            b.FlatAppearance.BorderColor = primary ? Accent : Border;
            return b;
        }

        static Stream Resource(string name)
        {
            return Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
        }

        static void Extract(string name, string to)
        {
            using (var s = Resource(name))
            using (var f = File.Create(to)) { s.CopyTo(f); }
        }

        void Append(string line, Color color)
        {
            if (InvokeRequired) { BeginInvoke(new Action<string, Color>(Append), line, color); return; }
            log.SelectionStart = log.TextLength;
            log.SelectionColor = color;
            log.AppendText(line + Environment.NewLine);
            log.ScrollToCaret();
        }

        void Run()
        {
            install.Enabled = browse.Enabled = folder.Enabled = shortcut.Enabled = browser.Enabled = winget.Enabled = false;
            log.Clear();
            status.Text = "Installing… this takes a few minutes the first time.";
            work = Path.Combine(Path.GetTempPath(), "OmniBotsSetup-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(work);
            string script = Path.Combine(work, "install.ps1"), bundle = Path.Combine(work, "omnibots.bundle");
            try {
                Extract("install.ps1", script);
                Extract("omnibots.bundle", bundle);
            } catch (Exception ex) {
                Done(false, "Couldn't unpack the installer: " + ex.Message);
                return;
            }
            installedDir = folder.Text.Trim();
            var args = new StringBuilder("-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"");
            args.Append(" -InstallDir \"" + installedDir.TrimEnd('\\') + "\" -Source \"" + bundle + "\" -Branch master");
            if (!shortcut.Checked) args.Append(" -NoShortcut");
            if (!browser.Checked) args.Append(" -NoBrowser");
            if (winget.Checked) args.Append(" -Yes");
            var psi = new ProcessStartInfo("powershell.exe", args.ToString()) {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                RedirectStandardInput = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            Append("Installing OmniBots " + Info.AppVersion + " into " + installedDir, Cyan);
            proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            proc.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) Append(e.Data, Text1); };
            proc.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) Append(e.Data, Color.FromArgb(255, 170, 120)); };
            proc.Exited += delegate {
                int code = proc.ExitCode;
                BeginInvoke(new Action(delegate { Done(code == 0, code == 0 ? "" : "The install stopped (exit code " + code + "). The messages above say why."); }));
            };
            try {
                proc.Start();
                proc.StandardInput.Close();              // no questions on a console nobody sees: -Yes answers them
                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
            } catch (Exception ex) {
                Done(false, "Couldn't start PowerShell: " + ex.Message);
            }
        }

        void Done(bool ok, string why)
        {
            if (ok) {
                status.Text = "OmniBots " + Info.AppVersion + " is installed.";
                status.ForeColor = Color.FromArgb(58, 208, 122);
                install.Visible = false;
                launch.Visible = true;
            } else {
                status.Text = why;
                status.ForeColor = Color.FromArgb(255, 90, 110);
                install.Text = "Try again";
                install.Enabled = browse.Enabled = folder.Enabled = shortcut.Enabled = browser.Enabled = winget.Enabled = true;
            }
            Cleanup();
        }

        void Launch()
        {
            string pyw = Path.Combine(installedDir, ".venv", "Scripts", "pythonw.exe");
            try {
                Process.Start(new ProcessStartInfo(pyw, "-m omnibots") { WorkingDirectory = installedDir, UseShellExecute = false });
                Close();
            } catch (Exception ex) {
                status.Text = "Couldn't start it: " + ex.Message;
            }
        }

        void Cleanup()
        {
            try { if (work != null && Directory.Exists(work)) Directory.Delete(work, true); } catch { }
        }

        [STAThread]
        static void Main(string[] argv)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (argv.Length > 0 && argv[0] == "--version") {
                Console.WriteLine("OmniBots Setup " + Info.InstallerVersion + " (OmniBots " + Info.AppVersion + ")");
                return;
            }
            Application.Run(new SetupForm());
        }
    }
}
