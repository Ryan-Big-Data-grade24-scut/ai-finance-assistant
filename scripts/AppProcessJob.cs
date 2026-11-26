using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

// The launcher owns the job handle. Closing it stops the servers and their descendants.
public sealed class AppProcessJob : IDisposable
{
    const uint KillOnJobClose = 0x2000;
    const uint ExtendedStartupInfoPresent = 0x00080000;
    const uint CreateNoWindow = 0x08000000;
    const int JobListAttribute = 0x0002000D;
    readonly SafeFileHandle job;

    public AppProcessJob()
    {
        job = RequireHandle(CreateJobObject(IntPtr.Zero, null), "Create process job");
        var limits = new JobLimits();
        limits.Basic.LimitFlags = KillOnJobClose;
        if (!SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf(typeof(JobLimits))))
        {
            var error = Failure("Set job shutdown policy");
            job.Dispose();
            throw error;
        }
    }

    public Process Start(string exe, string[] args, string directory, string output, string error)
    {
        var security = new SecurityAttributes
        {
            Size = Marshal.SizeOf(typeof(SecurityAttributes)), Inherit = true
        };
        using (var stdin = RequireHandle(CreateFile("NUL", 0x80000000, 3, ref security, 3, 0, IntPtr.Zero), "Open NUL"))
        using (var stdout = RequireHandle(CreateFile(output, 0x40000000, 7, ref security, 2, 0, IntPtr.Zero), "Open stdout log"))
        using (var stderr = RequireHandle(CreateFile(error, 0x40000000, 7, ref security, 2, 0, IntPtr.Zero), "Open stderr log"))
        {
            var size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            if (size == IntPtr.Zero) throw Failure("Get startup attribute size");
            var attributes = Marshal.AllocHGlobal(size);
            var jobList = Marshal.AllocHGlobal(IntPtr.Size);
            bool initialized = false;
            try
            {
                initialized = InitializeProcThreadAttributeList(attributes, 1, 0, ref size);
                if (!initialized) throw Failure("Initialize startup attributes");
                Marshal.WriteIntPtr(jobList, job.DangerousGetHandle());
                // Assign the process to the job before it can run or spawn children.
                if (!UpdateProcThreadAttribute(attributes, 0, new IntPtr(JobListAttribute), jobList,
                                               new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero))
                    throw Failure("Set child process job");

                var info = new StartupInfoEx
                {
                    Attributes = attributes,
                    Startup = new StartupInfo {
                        Size = Marshal.SizeOf(typeof(StartupInfoEx)), Flags = 0x100,
                        Input = stdin.DangerousGetHandle(), Output = stdout.DangerousGetHandle(),
                        Error = stderr.DangerousGetHandle()
                    }
                };
                var command = new StringBuilder(Quote(exe));
                foreach (var arg in args) command.Append(' ').Append(Quote(arg));
                ProcessInfo process;
                if (!CreateProcess(exe, command, IntPtr.Zero, IntPtr.Zero, true, ExtendedStartupInfoPresent | CreateNoWindow,
                                   IntPtr.Zero, directory, ref info, out process))
                    throw Failure("Start " + exe);
                CloseHandle(process.Thread);
                try { return Process.GetProcessById(process.Id); }
                finally { CloseHandle(process.Process); }
            }
            finally
            {
                if (initialized) DeleteProcThreadAttributeList(attributes);
                Marshal.FreeHGlobal(jobList);
                Marshal.FreeHGlobal(attributes);
            }
        }
    }

    public bool Contains(int pid)
    {
        using (var process = OpenProcess(0x1000, false, pid))
        {
            bool member;
            return !process.IsInvalid && IsProcessInJob(process, job, out member) && member;
        }
    }

    public void Dispose() { job.Dispose(); }

    static Win32Exception Failure(string action)
    {
        int code = Marshal.GetLastWin32Error();
        return new Win32Exception(code, action + ": " + new Win32Exception(code).Message);
    }

    static SafeFileHandle RequireHandle(SafeFileHandle handle, string action)
    {
        if (!handle.IsInvalid) return handle;
        var error = Failure(action);
        handle.Dispose();
        throw error;
    }

    static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
            result.Append(c);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits
    {
        public long ProcessTime, JobTime;
        public uint LimitFlags;
        public UIntPtr MinWorkingSet, MaxWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JobLimits
    {
        public BasicLimits Basic;
        public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct SecurityAttributes
    {
        public int Size;
        public IntPtr Descriptor;
        [MarshalAs(UnmanagedType.Bool)] public bool Inherit;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct StartupInfo
    {
        public int Size;
        public IntPtr Reserved, Desktop, Title;
        public uint X, Y, Width, Height, CharsX, CharsY, Fill, Flags;
        public ushort ShowWindow, ReservedSize;
        public IntPtr ReservedData, Input, Output, Error;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct StartupInfoEx
    {
        public StartupInfo Startup;
        public IntPtr Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo
    {
        public IntPtr Process, Thread;
        public int Id, ThreadId;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern SafeFileHandle CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(SafeFileHandle job, int type, ref JobLimits data, int length);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern SafeFileHandle CreateFile(string name, uint access, uint share, ref SecurityAttributes security,
                                            uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value,
                                                   IntPtr size, IntPtr previous, IntPtr returned);
    [DllImport("kernel32.dll")]
    static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string exe, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity,
                                      bool inheritHandles, uint flags, IntPtr environment, string directory,
                                      ref StartupInfoEx startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern SafeFileHandle OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsProcessInJob(SafeFileHandle process, SafeFileHandle job, out bool member);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
}
