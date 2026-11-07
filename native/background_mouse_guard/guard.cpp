// Thread-specific, operation-scoped AIR leave guard. No IAT/API interception.
#include <windows.h>
#include <stdint.h>
#include <new>
#include <cwchar>

struct SharedLease {
    volatile LONG active, ready, drained, filtered;
    DWORD owner, target, thread;
    ULONGLONG ownerCreated;
    HWND window;
    ULONG_PTR token;
};
#pragma data_seg(".maa_shared")
#pragma bss_seg(".maa_shared")
static SharedLease leases[16] = {};
#pragma data_seg()
#pragma bss_seg()
#pragma comment(linker, "/SECTION:.maa_shared,RWS")
static HMODULE module;
static constexpr WPARAM handshake = 0x4d41414d475631;
static constexpr WPARAM drainQueue = handshake + 1;
static constexpr ULONGLONG drainTimeoutMs = 5000;
struct Session { int slot; HANDLE process, mutex; HHOOK hook; DWORD ownerThread; ULONG_PTR token; };

static ULONGLONG created(HANDLE process) {
    FILETIME start, end, kernel, user;
    if (!GetProcessTimes(process, &start, &end, &kernel, &user)) return 0;
    return (ULONGLONG(start.dwHighDateTime) << 32) | start.dwLowDateTime;
}
static bool alive(const SharedLease& lease) {
    HANDLE process = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, lease.owner);
    if (!process) return false;
    bool valid = WaitForSingleObject(process, 0) == WAIT_TIMEOUT && created(process) == lease.ownerCreated;
    CloseHandle(process);
    return valid;
}
extern "C" __declspec(dllexport) LRESULT CALLBACK MaaMouseGuardHook(int code, WPARAM wp, LPARAM lp) {
    if (code >= 0 && wp == PM_REMOVE) {
        MSG* message = reinterpret_cast<MSG*>(lp);
        if (message->message == WM_MOUSELEAVE || (message->message == WM_NULL
            && (message->wParam == handshake || message->wParam == drainQueue))) {
            for (auto& lease : leases) {
                if (InterlockedCompareExchange(&lease.active, 1, 1) != 1 || lease.window != message->hwnd
                    || lease.target != GetCurrentProcessId() || lease.thread != GetCurrentThreadId()) continue;
                if (!alive(lease)) continue;
                if (message->message == WM_NULL) {
                    if (static_cast<ULONG_PTR>(message->lParam) == lease.token && GetModuleHandleW(L"Adobe AIR.dll")) {
                        if (message->wParam == handshake) InterlockedExchange(&lease.ready, 1);
                        else InterlockedExchange(&lease.drained, 1);
                    }
                } else if (InterlockedCompareExchange(&lease.ready, 1, 1)) {
                    InterlockedIncrement(&lease.filtered);
                    message->message = WM_NULL;
                    message->wParam = message->lParam = 0;
                }
                break;
            }
        }
    }
    return CallNextHookEx(nullptr, code, wp, lp);
}
extern "C" __declspec(dllexport) unsigned MaaMouseGuardVersion() { return 1; }

static bool sameTarget(Session* session, SharedLease& lease) {
    DWORD pid = 0;
    return WaitForSingleObject(session->process, 0) == WAIT_TIMEOUT
        && GetWindowThreadProcessId(lease.window, &pid) == lease.thread && pid == lease.target;
}
extern "C" __declspec(dllexport) BOOL MaaMouseGuardEnd(void* value, DWORD* filtered) {
    auto session = static_cast<Session*>(value);
    if (!session || session->ownerThread != GetCurrentThreadId()) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    auto& lease = leases[session->slot];
    if (lease.token != session->token || lease.owner != GetCurrentProcessId()) { SetLastError(ERROR_INVALID_DATA); return FALSE; }
    DWORD error = ERROR_SUCCESS;
    // PostMessage playback may still have its UP in the target queue. Keep
    // filtering until a marker behind those inputs is removed by that thread.
    if (lease.ready && sameTarget(session, lease)) {
        if (!PostMessageW(lease.window, WM_NULL, drainQueue, static_cast<LPARAM>(lease.token))) error = GetLastError();
        // UP can start a level load on AIR's UI thread. A one-second drain
        // deadline misclassified short loading stalls as cleanup failures.
        // Healthy queues acknowledge immediately; persistent stalls still fail.
        ULONGLONG deadline = GetTickCount64() + drainTimeoutMs;
        while (!error && !lease.drained && GetTickCount64() < deadline && sameTarget(session, lease)) Sleep(1);
        if (!lease.drained && !error && sameTarget(session, lease)) error = ERROR_TIMEOUT;
    }
    InterlockedExchange(&lease.active, 2); // Reserved until all cleanup has finished.
    DWORD count = InterlockedCompareExchange(&lease.filtered, 0, 0);
    bool valid = sameTarget(session, lease);
    BOOL removed = UnhookWindowsHookEx(session->hook);
    if (!removed && valid && !error) error = GetLastError();
    // Reconcile a dropped leave only after the complete action/release dwell.
    if (count && valid) {
        POINT cursor;
        if (GetCursorPos(&cursor)) {
            HWND under = WindowFromPoint(cursor);
            if (under != lease.window && !IsChild(lease.window, under)
                && !PostMessageW(lease.window, WM_MOUSELEAVE, 0, 0) && !error) error = GetLastError();
        } else if (!error) error = GetLastError();
    }
    if (filtered) *filtered = count;
    CloseHandle(session->process);
    ReleaseMutex(session->mutex); CloseHandle(session->mutex);
    InterlockedExchange(&lease.active, 0);
    delete session;
    SetLastError(error); return error == ERROR_SUCCESS;
}
extern "C" __declspec(dllexport) void* MaaMouseGuardBegin(HWND window) {
    DWORD pid = 0, thread = GetWindowThreadProcessId(window, &pid);
    wchar_t cls[128] = {};
    GetClassNameW(window, cls, 128);
    if (!thread || pid == GetCurrentProcessId() || wcscmp(cls, L"ApolloRuntimeContentWindow")) {
        SetLastError(ERROR_INVALID_WINDOW_HANDLE); return nullptr;
    }
    HANDLE process = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return nullptr;
    wchar_t name[128];
    swprintf_s(name, L"Local\\MAAZMXY4.MouseGuard.v1.%lu.%llu", pid, (unsigned long long)window);
    HANDLE mutex = CreateMutexW(nullptr, FALSE, name);
    if (!mutex) { CloseHandle(process); return nullptr; }
    DWORD wait = WaitForSingleObject(mutex, 0);
    if (wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED) {
        CloseHandle(mutex); CloseHandle(process); SetLastError(ERROR_BUSY); return nullptr;
    }
    HANDLE registry = CreateMutexW(nullptr, FALSE, L"Local\\MAAZMXY4.MouseGuard.Registry.v1");
    wait = registry ? WaitForSingleObject(registry, 1000) : WAIT_FAILED;
    int slot = -1;
    if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED) {
        bool occupied = false;
        for (auto& lease : leases) {
            if (lease.active && lease.target == pid && lease.window == window && alive(lease)) occupied = true;
        }
        if (!occupied) for (int i = 0; i < 16; ++i) {
            if (!leases[i].active || !alive(leases[i])) { slot = i; break; }
        }
        if (slot >= 0) {
            auto& lease = leases[slot];
            lease.active = 0; lease.ready = 0; lease.drained = 0; lease.filtered = 0;
            lease.owner = GetCurrentProcessId(); lease.ownerCreated = created(GetCurrentProcess());
            lease.target = pid; lease.thread = thread; lease.window = window;
            LARGE_INTEGER tick; QueryPerformanceCounter(&tick); lease.token = static_cast<ULONG_PTR>(tick.QuadPart);
            InterlockedExchange(&lease.active, 1);
        }
        ReleaseMutex(registry);
    }
    if (registry) CloseHandle(registry);
    if (slot < 0) { ReleaseMutex(mutex); CloseHandle(mutex); CloseHandle(process); SetLastError(ERROR_BUSY); return nullptr; }
    auto& lease = leases[slot];
    HHOOK hook = SetWindowsHookExW(WH_GETMESSAGE, MaaMouseGuardHook, module, thread);
    Session* session = hook ? new(std::nothrow) Session{slot,process,mutex,hook,GetCurrentThreadId(),lease.token} : nullptr;
    if (!session) {
        DWORD error = hook ? ERROR_OUTOFMEMORY : GetLastError();
        lease.active = 0; if (hook) UnhookWindowsHookEx(hook);
        ReleaseMutex(mutex); CloseHandle(mutex); CloseHandle(process); SetLastError(error); return nullptr;
    }
    bool posted = PostMessageW(window, WM_NULL, handshake, static_cast<LPARAM>(lease.token));
    ULONGLONG deadline = GetTickCount64() + 1000;
    while (posted && !lease.ready && GetTickCount64() < deadline && sameTarget(session, lease)) Sleep(1);
    if (!lease.ready || !sameTarget(session, lease)) {
        MaaMouseGuardEnd(session, nullptr); SetLastError(ERROR_TIMEOUT); return nullptr;
    }
    return session;
}
BOOL WINAPI DllMain(HMODULE current, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) { module = current; DisableThreadLibraryCalls(current); }
    return TRUE;
}
