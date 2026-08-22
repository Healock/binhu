#include <windows.h>
#include <shlobj.h>

#include <string>
#include <vector>

namespace {

std::wstring quote(const std::wstring& value) {
    return L"\"" + value + L"\"";
}

std::wstring module_directory() {
    std::vector<wchar_t> buffer(32768);
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (length == 0 || length >= buffer.size()) return {};
    std::wstring path(buffer.data(), length);
    const size_t separator = path.find_last_of(L"\\/");
    return separator == std::wstring::npos ? std::wstring() : path.substr(0, separator);
}

void show_error(const wchar_t* message) {
    MessageBoxW(nullptr, message, L"滨湖智慧平台", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    const std::wstring app_directory = module_directory();
    if (app_directory.empty()) {
        show_error(L"无法定位客户端安装目录。");
        return 10;
    }

    wchar_t program_files[MAX_PATH] = {};
    if (FAILED(SHGetFolderPathW(nullptr, CSIDL_PROGRAM_FILES, nullptr, SHGFP_TYPE_CURRENT, program_files))) {
        show_error(L"无法定位 Program Files 目录。");
        return 11;
    }

    const std::wstring loader = std::wstring(program_files) + L"\\VxKex\\VxKexLdr.exe";
    const std::wstring electron = app_directory + L"\\BinhuWin7.exe";
    if (GetFileAttributesW(loader.c_str()) == INVALID_FILE_ATTRIBUTES) {
        show_error(L"未找到 VxKex。请重新运行滨湖 Win7 安装程序进行修复。");
        return 12;
    }
    if (GetFileAttributesW(electron.c_str()) == INVALID_FILE_ATTRIBUTES) {
        show_error(L"客户端运行时不完整。请重新安装滨湖智慧平台。");
        return 13;
    }

    std::wstring command = quote(loader) + L" " + quote(electron) + L" --disable-direct-composition";
    std::vector<wchar_t> mutable_command(command.begin(), command.end());
    mutable_command.push_back(L'\0');

    STARTUPINFOW startup = {};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process = {};
    const BOOL started = CreateProcessW(
        nullptr,
        mutable_command.data(),
        nullptr,
        nullptr,
        FALSE,
        CREATE_UNICODE_ENVIRONMENT,
        nullptr,
        app_directory.c_str(),
        &startup,
        &process);
    if (!started) {
        show_error(L"客户端启动失败。请确认 VxKex 已正确安装，并查看系统事件日志。");
        return static_cast<int>(GetLastError());
    }

    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
