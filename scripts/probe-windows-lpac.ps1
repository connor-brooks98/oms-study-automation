# Network-free, one-child Windows LPAC acceptance probe. Retains every new fixture/profile.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$FixtureParent,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^oms-lpac-[0-9a-f]{32}$')][string]$ProfileName
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -ne 'Desktop') {
    throw 'Run only in an existing 64-bit Windows PowerShell process.'
}
# Refuse reused paths and junction/symlink ancestors before creating any fixture.
$parent = [IO.Path]::GetFullPath($FixtureParent)
if ($parent -notmatch '^[A-Za-z]:\\' -or -not [IO.Directory]::Exists($parent)) { throw 'FixtureParent must be an existing local directory.' }
$ancestor = [IO.DirectoryInfo]$parent
while ($null -ne $ancestor) {
    if (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse-point ancestors are forbidden.' }
    $ancestor = $ancestor.Parent
}
$fixture = Join-Path $parent $ProfileName
if (Test-Path -LiteralPath $fixture) { throw 'Fixture already exists; use a new profile name. No retry.' }
$null = New-Item -ItemType Directory -Path $fixture
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$privateAcl = [Security.AccessControl.DirectorySecurity]::new()
$privateAcl.SetAccessRuleProtection($true, $false)
$privateAcl.SetOwner($owner)
$privateAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $owner, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $fixture -AclObject $privateAcl
$compilerTemp = Join-Path $fixture 'compiler'
$null = New-Item -ItemType Directory -Path $compilerTemp
$savedTemp = $env:TEMP
$savedTmp = $env:TMP
$report = [ordered]@{ status = 'failed'; fixture = $fixture; profile_name = $ProfileName; native_result = $null; error = $null }
try {
    # Add-Type's temporary compiler files belong to this fixture too.
    $env:TEMP = $compilerTemp
    $env:TMP = $compilerTemp
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

public sealed class OmsLpacResult {
    public string Stage = "initial";
    public string Error;
    public int? NativeErrorCode;
    public string StartedUtc = DateTime.UtcNow.ToString("o");
    public string FinishedUtc;
    public string ProfileSid;
    public string ChildSid;
    public string CallerSid;
    public string Executable;
    public string CommandLine;
    public string ChildLocalAppData;
    public uint ProcessId;
    public int IsAppContainer;
    public bool CallerAccessCheckReturned, CallerAccessStatus;
    public uint CallerGrantedAccess;
    public bool ChildAccessCheckReturned, ChildAccessStatus;
    public uint ChildGrantedAccess;
    public int CapabilityCount;
    public string[] CapabilitySids;
    public uint[] CapabilityAttributes;
    public int SessionId;
    public bool ProfileCreated;
    public bool Resumed;
    public bool TimedOut;
    public bool KillAttempted;
    public bool Reaped;
    public uint? ExitCode;
    public bool Passed;
}

public static class OmsLpacProbe {
    // Documented TOKEN_INFORMATION_CLASS values (winnt.h).
    const int TokenElevation = 20, TokenIsAppContainer = 29, TokenCapabilities = 30;
    const int TokenAppContainerSid = 31;
    // Microsoft's documented LPAC cmd.exe startup requirements, fixed to this synthetic probe.
    // https://raw.githubusercontent.com/microsoft/SandboxSecurityTools/f6263b76dbf77305969aa599ff8a66579aae5a54/LaunchAppContainer/README.md
    static readonly string[] CmdCapabilitySids = {
        "S-1-15-3-1024-2405443489-874036122-4286035555-1823921565-1746547431-2453885448-3625952902-991631256", // lpacCom
        "S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-432734479-3232135806-4053264122-3456934681" // registryRead
    };
    [StructLayout(LayoutKind.Sequential)] struct SidAndAttributes {
        public IntPtr Sid; public uint Attributes;
    }
    [StructLayout(LayoutKind.Sequential)] struct TokenGroupsHeader {
        public uint Count; public SidAndAttributes First;
    }
    const uint WAIT_TIMEOUT = 258, INFINITE_ERROR = 0xffffffff;
    [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes {
        public int Length; public IntPtr Descriptor; public int Inherit;
    }
    [StructLayout(LayoutKind.Sequential)] struct SecurityCapabilities {
        public IntPtr Sid, Capabilities; public uint Count, Reserved;
    }
    [StructLayout(LayoutKind.Sequential)] struct GenericMapping {
        public uint Read, Write, Execute, All;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct StartupInfo {
        public int cb; public string Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
        public ushort ShowWindow, ReservedBytes; public IntPtr ReservedPointer;
        public IntPtr StdInput, StdOutput, StdError;
    }
    [StructLayout(LayoutKind.Sequential)] struct StartupInfoEx {
        public StartupInfo StartupInfo; public IntPtr AttributeList;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInformation {
        public IntPtr Process, Thread; public uint ProcessId, ThreadId;
    }
    [DllImport("userenv.dll", CharSet = CharSet.Unicode)] static extern int CreateAppContainerProfile(
        string name, string displayName, string description, IntPtr capabilities, uint count, out IntPtr sid);
    [DllImport("advapi32.dll")] static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int length, out int needed);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool DuplicateTokenEx(
        IntPtr token, uint access, IntPtr attributes, int level, int type, out IntPtr duplicate);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool AccessCheck(
        IntPtr descriptor, IntPtr token, uint desired, ref GenericMapping mapping,
        IntPtr privileges, ref uint privilegeLength, out uint granted, out bool status);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returnedSize);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool CreateProcessW(
        string application, StringBuilder command, IntPtr processAttributes, IntPtr threadAttributes,
        bool inherit, uint flags, IntPtr environment, string cwd, ref StartupInfoEx startup, out ProcessInformation process);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateFileW(
        string path, uint access, uint share, ref SecurityAttributes security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);

    static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
    static void Win32(bool ok, int error, string operation) {
        if (!ok) throw new Win32Exception(error, operation + " failed with Win32 error " + error);
    }
    static IntPtr TokenData(IntPtr token, int kind, out int length) {
        length = 0;
        int needed;
        bool first = GetTokenInformation(token, kind, IntPtr.Zero, 0, out needed);
        int sizeError = Marshal.GetLastWin32Error();
        int minimum = kind == TokenAppContainerSid ? IntPtr.Size : 4;
        Require(!first && (sizeError == 24 || sizeError == 122) && needed >= minimum && needed <= 65536,
            "Token size query: kind=" + kind + ", return=" + first + ", error=" + sizeError + ", needed=" + needed);
        IntPtr data = Marshal.AllocHGlobal(needed);
        int capacity = needed;
        try {
            bool ok = GetTokenInformation(token, kind, data, capacity, out needed);
            int error = Marshal.GetLastWin32Error();
            Require(ok && needed >= minimum && needed <= capacity,
                "Token data query: kind=" + kind + ", return=" + ok + ", error=" + error + ", needed=" + needed);
            length = needed;
            return data;
        }
        catch { Marshal.FreeHGlobal(data); throw; }
    }
    static int TokenInt(IntPtr token, int kind) {
        // These information classes return a documented DWORD, not a variable TOKEN_GROUPS.
        IntPtr data = Marshal.AllocHGlobal(4);
        try {
            int needed;
            bool ok = GetTokenInformation(token, kind, data, 4, out needed);
            int error = Marshal.GetLastWin32Error();
            Require(ok && needed == 4,
                "Token DWORD query: kind=" + kind + ", return=" + ok + ", error=" + error + ", needed=" + needed);
            return Marshal.ReadInt32(data);
        } finally { Marshal.FreeHGlobal(data); }
    }
    static string TokenSid(IntPtr token) {
        int length;
        IntPtr data = TokenData(token, TokenAppContainerSid, out length);
        try {
            IntPtr sid = Marshal.ReadIntPtr(data);
            Require(sid != IntPtr.Zero, "Child has no AppContainer SID.");
            return new SecurityIdentifier(sid).Value;
        } finally { Marshal.FreeHGlobal(data); }
    }
    static void VerifyCmdCapabilities(IntPtr token, OmsLpacResult result) {
        int length;
        IntPtr data = TokenData(token, TokenCapabilities, out length);
        try {
            result.CapabilityCount = Marshal.ReadInt32(data);
            Require(result.CapabilityCount == CmdCapabilitySids.Length, "Unexpected capability count.");
            int first = Marshal.OffsetOf(typeof(TokenGroupsHeader), "First").ToInt32();
            int stride = Marshal.SizeOf(typeof(SidAndAttributes));
            Require(length >= first + stride * result.CapabilityCount, "Truncated capability array.");
            result.CapabilitySids = new string[result.CapabilityCount];
            result.CapabilityAttributes = new uint[result.CapabilityCount];
            bool[] seen = new bool[CmdCapabilitySids.Length];
            long start = data.ToInt64(), end = checked(start + length);
            for (int i = 0; i < result.CapabilityCount; i++) {
                SidAndAttributes entry = (SidAndAttributes)Marshal.PtrToStructure(
                    IntPtr.Add(data, first + stride * i), typeof(SidAndAttributes));
                long address = entry.Sid.ToInt64();
                Require(address >= start && address <= end - 8, "Capability SID outside returned buffer.");
                int sidLength = 8 + 4 * Marshal.ReadByte(entry.Sid, 1);
                Require(address <= end - sidLength, "Truncated capability SID.");
                string actual = new SecurityIdentifier(entry.Sid).Value;
                result.CapabilitySids[i] = actual;
                result.CapabilityAttributes[i] = entry.Attributes;
                int expected = Array.IndexOf(CmdCapabilitySids, actual);
                Require(expected >= 0 && !seen[expected] && entry.Attributes == 4,
                    "Unexpected, duplicate or non-enabled capability: " + actual + " attributes=" + entry.Attributes);
                seen[expected] = true;
            }
        } finally { Marshal.FreeHGlobal(data); }
    }
    static void DuplicateForAccessCheck(IntPtr token, out IntPtr duplicate) {
        // TOKEN_QUERY, SecurityIdentification, TokenImpersonation; never impersonate a thread.
        bool ok = DuplicateTokenEx(token, 8, IntPtr.Zero, 1, 2, out duplicate);
        int error = Marshal.GetLastWin32Error();
        Win32(ok, error, "Duplicate token for AccessCheck");
    }
    static void CheckLpacAccess(IntPtr token, out bool returned, out bool status, out uint granted) {
        // Chromium CheckLpacToken: in-memory descriptor only, never applied to a filesystem object.
        // https://chromium.googlesource.com/chromium/src/+/04d774d3827c8532b1b7d3966629f9193a35dd0e/sandbox/win/src/app_container_test.cc
        RawSecurityDescriptor descriptor = new RawSecurityDescriptor(
            "O:SYG:SYD:(A;;0x3;;;WD)(A;;0x1;;;S-1-15-2-1)(A;;0x2;;;S-1-15-2-2)");
        byte[] bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        IntPtr data = IntPtr.Zero, privileges = IntPtr.Zero;
        try {
            data = Marshal.AllocHGlobal(bytes.Length);
            Marshal.Copy(bytes, 0, data, bytes.Length);
            // Bounded privilege buffer; an insufficient buffer fails closed without replay.
            uint privilegeLength = 1024;
            privileges = Marshal.AllocHGlobal((int)privilegeLength);
            GenericMapping mapping = new GenericMapping();
            returned = AccessCheck(data, token, 0x02000000, ref mapping, privileges,
                ref privilegeLength, out granted, out status); // MAXIMUM_ALLOWED
            int error = Marshal.GetLastWin32Error();
            Win32(returned, error, "LPAC AccessCheck");
            Require(privilegeLength <= 1024, "AccessCheck privilege result exceeds its buffer.");
        } finally {
            if (privileges != IntPtr.Zero) Marshal.FreeHGlobal(privileges);
            if (data != IntPtr.Zero) Marshal.FreeHGlobal(data);
        }
    }
    static void Attribute(IntPtr list, long key, IntPtr value, int size) {
        bool ok = UpdateProcThreadAttribute(list, 0, new IntPtr(key), value, new IntPtr(size), IntPtr.Zero, IntPtr.Zero);
        int error = Marshal.GetLastWin32Error();
        Win32(ok, error, "UpdateProcThreadAttribute " + key);
    }
    static IntPtr StdioFile(string path, bool input) {
        SecurityAttributes security = new SecurityAttributes { Length = Marshal.SizeOf(typeof(SecurityAttributes)), Inherit = 1 };
        IntPtr handle = CreateFileW(path, input ? 0x80000000u : 0x40000000u, 1, ref security, 1, 0x80, IntPtr.Zero);
        int error = Marshal.GetLastWin32Error();
        Win32(handle != new IntPtr(-1), error, "Create new stdio file");
        return handle;
    }
    static void GrantDirectory(string path, SecurityIdentifier sid, FileSystemRights rights, InheritanceFlags inheritance) {
        DirectorySecurity acl = Directory.GetAccessControl(path);
        acl.AddAccessRule(new FileSystemAccessRule(sid, rights, inheritance, PropagationFlags.None, AccessControlType.Allow));
        Directory.SetAccessControl(path, acl);
    }

    public static OmsLpacResult Run(string fixture, string profileName) {
        OmsLpacResult result = new OmsLpacResult();
        IntPtr sid = IntPtr.Zero, callerToken = IntPtr.Zero, childToken = IntPtr.Zero;
        IntPtr callerCheckToken = IntPtr.Zero, childCheckToken = IntPtr.Zero;
        IntPtr attributes = IntPtr.Zero, capsData = IntPtr.Zero, policy = IntPtr.Zero, handlesData = IntPtr.Zero, environment = IntPtr.Zero;
        IntPtr stdin = IntPtr.Zero, stdout = IntPtr.Zero, stderr = IntPtr.Zero;
        IntPtr capabilityEntries = IntPtr.Zero;
        IntPtr[] capabilitySids = new IntPtr[CmdCapabilitySids.Length];
        ProcessInformation process = new ProcessInformation();
        bool attributesInitialized = false;
        string inside = Path.Combine(fixture, "inside");
        string stdoutPath = Path.Combine(fixture, "child.stdout");
        string stderrPath = Path.Combine(fixture, "child.stderr");
        string insideMarker = "OMS_LPAC_INSIDE_" + Guid.NewGuid().ToString("N");
        string outsideMarker = "OMS_LPAC_OUTSIDE_" + Guid.NewGuid().ToString("N");
        try {
            result.Stage = "caller";
            bool opened = OpenProcessToken(GetCurrentProcess(), 10, out callerToken); // QUERY | DUPLICATE
            int openError = Marshal.GetLastWin32Error();
            Win32(opened, openError, "Open caller token");
            Require(TokenInt(callerToken, TokenElevation) == 0, "Elevated caller forbidden.");
            result.CallerSid = WindowsIdentity.GetCurrent().User.Value;
            result.Stage = "caller-access-control";
            Require(TokenInt(callerToken, TokenIsAppContainer) == 0, "Caller control must not be an AppContainer.");
            DuplicateForAccessCheck(callerToken, out callerCheckToken);
            CheckLpacAccess(callerCheckToken, out result.CallerAccessCheckReturned,
                out result.CallerAccessStatus, out result.CallerGrantedAccess);
            Require(result.CallerAccessStatus && result.CallerGrantedAccess == 3,
                "Ordinary caller AccessCheck control must grant exactly mask 3.");
            result.Stage = "child-environment";
            string localAppData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            Require(!String.IsNullOrWhiteSpace(localAppData) && localAppData.Length >= 3
                && Char.IsLetter(localAppData[0]) && localAppData[1] == ':' && localAppData[2] == '\\'
                && localAppData.IndexOf('\0') < 0, "Caller LOCALAPPDATA must be an absolute local path.");
            result.ChildLocalAppData = Path.GetFullPath(localAppData);
            result.Stage = "fixture";
            Directory.CreateDirectory(inside);
            File.WriteAllText(Path.Combine(inside, "inside.txt"), insideMarker + "\r\n", Encoding.ASCII);
            File.WriteAllText(Path.Combine(fixture, "outside.txt"), outsideMarker + "\r\n", Encoding.ASCII);
            result.Stage = "profile";
            int hr = CreateAppContainerProfile(profileName, profileName, "Retained network-free OMS LPAC probe", IntPtr.Zero, 0, out sid);
            // Never derive/reuse an existing profile, and never delete it on failure.
            if (hr != 0) Marshal.ThrowExceptionForHR(hr);
            Require(hr == 0, "Profile creation did not return S_OK.");
            Require(sid != IntPtr.Zero, "Profile returned no SID.");
            result.ProfileCreated = true;
            SecurityIdentifier packageSid = new SecurityIdentifier(sid);
            result.ProfileSid = packageSid.Value;
            File.WriteAllText(Path.Combine(fixture, "profile.txt"), profileName + "\r\n" + result.ProfileSid + "\r\n", Encoding.ASCII);
            result.Stage = "new-fixture-grants";
            GrantDirectory(fixture, packageSid, FileSystemRights.Traverse, InheritanceFlags.None);
            GrantDirectory(inside, packageSid, FileSystemRights.ReadAndExecute, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit);
            result.Stage = "stdio";
            stdin = StdioFile(Path.Combine(fixture, "child.stdin"), true);
            stdout = StdioFile(stdoutPath, false);
            stderr = StdioFile(stderrPath, false);
            result.Stage = "attributes";
            IntPtr size = IntPtr.Zero;
            bool sized = InitializeProcThreadAttributeList(IntPtr.Zero, 4, 0, ref size);
            int sizeError = Marshal.GetLastWin32Error();
            Require(!sized && sizeError == 122 && size.ToInt64() > 0,
                "Attribute size query: return=" + sized + ", error=" + sizeError + ", needed=" + size);
            attributes = Marshal.AllocHGlobal(size);
            bool initialized = InitializeProcThreadAttributeList(attributes, 4, 0, ref size);
            int initializeError = Marshal.GetLastWin32Error();
            Win32(initialized, initializeError, "Initialize attributes");
            attributesInitialized = true;
            int capabilityStride = Marshal.SizeOf(typeof(SidAndAttributes));
            capabilityEntries = Marshal.AllocHGlobal(capabilityStride * CmdCapabilitySids.Length);
            for (int i = 0; i < CmdCapabilitySids.Length; i++) {
                SecurityIdentifier capability = new SecurityIdentifier(CmdCapabilitySids[i]);
                byte[] bytes = new byte[capability.BinaryLength];
                capability.GetBinaryForm(bytes, 0);
                capabilitySids[i] = Marshal.AllocHGlobal(bytes.Length);
                Marshal.Copy(bytes, 0, capabilitySids[i], bytes.Length);
                Marshal.StructureToPtr(new SidAndAttributes { Sid = capabilitySids[i], Attributes = 4 },
                    IntPtr.Add(capabilityEntries, i * capabilityStride), false); // SE_GROUP_ENABLED
            }
            SecurityCapabilities caps = new SecurityCapabilities {
                Sid = sid, Capabilities = capabilityEntries, Count = (uint)CmdCapabilitySids.Length
            };
            capsData = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SecurityCapabilities)));
            Marshal.StructureToPtr(caps, capsData, false);
            Attribute(attributes, 0x20009, capsData, Marshal.SizeOf(typeof(SecurityCapabilities)));
            policy = Marshal.AllocHGlobal(4);
            Marshal.WriteInt32(policy, 1);
            // LPAC opt-out and no descendant processes. Both are mandatory; no fallback.
            Attribute(attributes, 0x2000f, policy, 4);
            Attribute(attributes, 0x2000e, policy, 4);
            handlesData = Marshal.AllocHGlobal(IntPtr.Size * 3);
            Marshal.WriteIntPtr(handlesData, 0, stdin);
            Marshal.WriteIntPtr(handlesData, IntPtr.Size, stdout);
            Marshal.WriteIntPtr(handlesData, IntPtr.Size * 2, stderr);
            Attribute(attributes, 0x20002, handlesData, IntPtr.Size * 3);
            result.Executable = Path.Combine(Environment.SystemDirectory, "cmd.exe");
            // Fixed relative filenames avoid caller-controlled shell text. TYPE is a cmd builtin.
            result.CommandLine = "\"" + result.Executable + "\" /d /v:off /c \"type inside.txt & type ..\\outside.txt\"";
            string windows = Directory.GetParent(Environment.SystemDirectory).FullName;
            // Single-variable hypothesis for error 203; Windows documents AppContainer LOCALAPPDATA rerouting.
            // https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer
            string env = "COMSPEC=" + result.Executable + "\0LOCALAPPDATA=" + result.ChildLocalAppData
                + "\0SystemRoot=" + windows + "\0TEMP=" + inside + "\0TMP=" + inside + "\0WINDIR=" + windows + "\0\0";
            environment = Marshal.StringToHGlobalUni(env);
            StartupInfoEx startup = new StartupInfoEx();
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(StartupInfoEx));
            startup.StartupInfo.Flags = 0x100; // STARTF_USESTDHANDLES
            startup.StartupInfo.StdInput = stdin; startup.StartupInfo.StdOutput = stdout; startup.StartupInfo.StdError = stderr;
            startup.AttributeList = attributes;
            result.Stage = "create-suspended";
            // EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED | CREATE_NO_WINDOW
            bool created = CreateProcessW(result.Executable, new StringBuilder(result.CommandLine), IntPtr.Zero, IntPtr.Zero, true,
                0x08080404, environment, inside, ref startup, out process);
            int createError = Marshal.GetLastWin32Error();
            Win32(created, createError, "Create LPAC cmd");
            result.ProcessId = process.ProcessId;
            result.Stage = "verify-suspended-token";
            bool childOpened = OpenProcessToken(process.Process, 10, out childToken); // QUERY | DUPLICATE
            int childOpenError = Marshal.GetLastWin32Error();
            Win32(childOpened, childOpenError, "Open child token");
            result.IsAppContainer = TokenInt(childToken, TokenIsAppContainer);
            VerifyCmdCapabilities(childToken, result);
            result.SessionId = TokenInt(childToken, 12);
            result.ChildSid = TokenSid(childToken);
            DuplicateForAccessCheck(childToken, out childCheckToken);
            CheckLpacAccess(childCheckToken, out result.ChildAccessCheckReturned,
                out result.ChildAccessStatus, out result.ChildGrantedAccess);
            File.WriteAllText(Path.Combine(fixture, "token.txt"), "pid=" + result.ProcessId
                + "\r\nTokenIsAppContainer=" + result.IsAppContainer
                + "\r\nCallerAccessCheck.Returned=" + result.CallerAccessCheckReturned
                + "\r\nCallerAccessCheck.AccessStatus=" + result.CallerAccessStatus
                + "\r\nCallerAccessCheck.GrantedMask=" + result.CallerGrantedAccess
                + "\r\nChildAccessCheck.Returned=" + result.ChildAccessCheckReturned
                + "\r\nChildAccessCheck.AccessStatus=" + result.ChildAccessStatus
                + "\r\nChildAccessCheck.GrantedMask=" + result.ChildGrantedAccess
                + "\r\nTokenCapabilities.Count=" + result.CapabilityCount
                + "\r\nTokenCapabilities.Sids=" + String.Join(";", result.CapabilitySids)
                + "\r\nTokenSessionId=" + result.SessionId
                + "\r\nTokenAppContainerSid=" + result.ChildSid + "\r\n", Encoding.ASCII);
            Require(result.IsAppContainer == 1 && result.ChildAccessStatus && result.ChildGrantedAccess == 2
                && result.CapabilityCount == CmdCapabilitySids.Length && result.ChildSid == result.ProfileSid && result.SessionId == 1,
                "Suspended token does not match the LPAC profile with the two fixed cmd capabilities.");
            result.Stage = "resume";
            uint previousCount = ResumeThread(process.Thread);
            int resumeError = Marshal.GetLastWin32Error();
            Win32(previousCount != INFINITE_ERROR, resumeError, "ResumeThread");
            Require(previousCount == 1, "Unexpected suspend count.");
            result.Resumed = true;
            result.Stage = "wait";
            uint wait = WaitForSingleObject(process.Process, 15000);
            int waitError = Marshal.GetLastWin32Error();
            Win32(wait != INFINITE_ERROR, waitError, "WaitForSingleObject");
            result.TimedOut = wait == WAIT_TIMEOUT;
            Require(wait == 0, "Child wait failed or exceeded 15 seconds: " + wait);
            result.Reaped = true;
            uint exit;
            bool exited = GetExitCodeProcess(process.Process, out exit);
            int exitError = Marshal.GetLastWin32Error();
            Win32(exited, exitError, "Child exit code");
            result.ExitCode = exit;
        } catch (Exception error) {
            result.Error = error.ToString();
            Win32Exception native = error as Win32Exception;
            if (native != null) result.NativeErrorCode = native.NativeErrorCode;
        }
        finally {
            if (process.Process != IntPtr.Zero && !result.Reaped) {
                result.KillAttempted = true;
                bool killed = TerminateProcess(process.Process, 0xe0000001);
                int killError = Marshal.GetLastWin32Error();
                uint cleanupWait = WaitForSingleObject(process.Process, 5000);
                int cleanupWaitError = Marshal.GetLastWin32Error();
                result.Reaped = cleanupWait == 0;
                uint exit;
                if (result.Reaped) {
                    bool gotExit = GetExitCodeProcess(process.Process, out exit);
                    int cleanupExitError = Marshal.GetLastWin32Error();
                    if (gotExit) result.ExitCode = exit;
                    else result.Error += "\nCleanup GetExitCodeProcess failed with Win32 error " + cleanupExitError;
                }
                if (!result.Reaped) result.Error += "\nOWNED CHILD NOT REAPED; TerminateProcess=" + killed + ", error=" + killError
                    + ", wait=" + cleanupWait + ", wait error=" + cleanupWaitError;
            }
            foreach (IntPtr handle in new IntPtr[] { childCheckToken, callerCheckToken, childToken, callerToken, process.Thread, process.Process, stdin, stdout, stderr }) {
                if (handle == IntPtr.Zero) continue;
                bool closed = CloseHandle(handle);
                int closeError = Marshal.GetLastWin32Error();
                if (!closed) result.Error += "\nCloseHandle failed with Win32 error " + closeError;
            }
            if (attributesInitialized) DeleteProcThreadAttributeList(attributes);
            foreach (IntPtr allocation in new IntPtr[] { attributes, capsData, capabilityEntries, policy, handlesData, environment })
                if (allocation != IntPtr.Zero) Marshal.FreeHGlobal(allocation);
            foreach (IntPtr capabilitySid in capabilitySids)
                if (capabilitySid != IntPtr.Zero) Marshal.FreeHGlobal(capabilitySid);
            if (sid != IntPtr.Zero) FreeSid(sid);
        }
        if (result.Error == null) {
            try {
                result.Stage = "assert-read-boundary";
                string output = File.ReadAllText(stdoutPath, Encoding.ASCII);
                string errors = File.ReadAllText(stderrPath, Encoding.ASCII);
                Require(result.ExitCode == 1 && output == insideMarker + "\r\n" && !output.Contains(outsideMarker)
                    && errors.Trim() == "Access is denied.", "Expected inside-only stdout, English Access is denied stderr and cmd exit 1.");
                Require(File.ReadAllText(Path.Combine(inside, "inside.txt"), Encoding.ASCII) == insideMarker + "\r\n"
                    && File.ReadAllText(Path.Combine(fixture, "outside.txt"), Encoding.ASCII) == outsideMarker + "\r\n", "Canary content changed.");
                result.Passed = true;
                result.Stage = "passed";
            } catch (Exception error) { result.Error = error.ToString(); }
        }
        result.FinishedUtc = DateTime.UtcNow.ToString("o");
        return result;
    }
}
'@
    $report.native_result = [OmsLpacProbe]::Run($fixture, $ProfileName)
    if ($report.native_result.Passed) { $report.status = 'passed' }
} catch { $report.error = $_.Exception.ToString() }
finally {
    $env:TEMP = $savedTemp
    $env:TMP = $savedTmp
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $fixture 'result.json') -Encoding UTF8
}
Write-Output (Join-Path $fixture 'result.json')
if ($report.status -ne 'passed') { throw 'LPAC probe failed; inspect retained result.json and raw child output. No retry or weaker fallback.' }
