// Hidden, isolated IPC fixture. Never opens or connects to a game.
#include <windows.h>
#include <fstream>
#include <cstdint>
static LONG counts[3][6] = {};
static LRESULT CALLBACK windowProc(HWND window, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_NCCREATE) SetWindowLongPtrW(window, GWLP_USERDATA,
        reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams));
    int index = static_cast<int>(GetWindowLongPtrW(window, GWLP_USERDATA));
    int bucket = -1;
    switch (msg) {
    case WM_MOUSELEAVE: bucket=0; break;
    case WM_LBUTTONDOWN: bucket=1; break;
    case WM_LBUTTONUP: bucket=2; break;
    case WM_MOUSEMOVE: bucket=3; break;
    case WM_KEYDOWN: bucket=4; break;
    case WM_APP+1: bucket=5; break;
    case WM_APP+2: PostQuitMessage(0); return 0;
    }
    if (bucket >= 0) ++counts[index][bucket];
    if (msg == WM_APP+3 && wp < 6) return counts[index][wp];
    return DefWindowProcW(window, msg, wp, lp);
}
int wmain(int argc, wchar_t** argv) {
    if (argc != 3 || !LoadLibraryW(argv[1])) return 2;
    WNDCLASSW cls = {}; cls.lpfnWndProc=windowProc; cls.hInstance=GetModuleHandleW(nullptr);
    cls.lpszClassName=L"ApolloRuntimeContentWindow";
    if (!RegisterClassW(&cls)) return 3;
    cls.lpszClassName=L"MaaGuardNonAirFixture";
    if (!RegisterClassW(&cls)) return 3;
    HWND windows[3];
    for (int i=0; i<3; ++i) {
        windows[i]=CreateWindowW(i==2 ? cls.lpszClassName : L"ApolloRuntimeContentWindow",
            L"MAA mouse guard unit fixture", WS_OVERLAPPED, 0,0,100,100,
            nullptr,nullptr,cls.hInstance,reinterpret_cast<void*>(static_cast<intptr_t>(i)));
        if (!windows[i]) return 4;
    }
    std::wstring temporary = std::wstring(argv[2])+L".tmp";
    std::ofstream ready(temporary);
    ready << "[" << reinterpret_cast<uintptr_t>(windows[0]) << ","
        << reinterpret_cast<uintptr_t>(windows[1]) << "," << reinterpret_cast<uintptr_t>(windows[2]) << "]";
    ready.close();
    if (!MoveFileExW(temporary.c_str(), argv[2], MOVEFILE_REPLACE_EXISTING)) return 5;
    MSG message;
    while (GetMessageW(&message,nullptr,0,0)>0) { TranslateMessage(&message); DispatchMessageW(&message); }
    return 0;
}
