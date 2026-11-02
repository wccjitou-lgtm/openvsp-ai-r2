//
// OpenVSP R2 AI Design Window implementation.
// Released under the NASA Open Source Agreement (NOSA) version 1.3.
//

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#include <winhttp.h>
#include <wincrypt.h>

#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "crypt32.lib")
#pragma comment(lib, "shell32.lib")

#include "AIDesignWindow.h"
#include "LocalizationMgr.h"

#include <FL/Fl.H>
#include <FL/Fl_Box.H>
#include <FL/Fl_Button.H>
#include <FL/Fl_Choice.H>
#include <FL/Fl_Check_Button.H>
#include <FL/Fl_Input.H>
#include <FL/Fl_Input_Choice.H>
#include <FL/Fl_Multiline_Input.H>
#include <FL/Fl_Native_File_Chooser.H>
#include <FL/Fl_Output.H>
#include <FL/Fl_Text_Display.H>
#include <FL/Fl_Return_Button.H>
#include <FL/fl_ask.H>

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cctype>
#include <cstring>
#include <ctime>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace
{

struct ProviderPreset
{
    const char* label;
    const char* id;
    const char* base_url;
    const char* default_model;
    const char* builtin_models;   // comma separated fallback list
};

const ProviderPreset kProviders[] =
{
    { "DeepSeek", "deepseek", "https://api.deepseek.com", "deepseek-flash",
      "deepseek-flash,deepseek-v4-pro" },
    { "GLM (智谱)", "glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3",
      "glm-5.3,glm-5.3-flash,glm-5.3-flashx,glm-5.2" },
    { "Kimi (月之暗面)", "kimi", "https://api.moonshot.cn/v1", "kimi-k3.1-pro",
      "kimi-k3.1-pro,kimi-k3.1-pro-highspeed,kimi-k3,kimi-k2.7-code,kimi-k2.6" },
    { "GPT (OpenAI)", "openai", "https://api.openai.com/v1", "gpt-6-astra",
      "gpt-6-astra,gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna" },
    { "自定义 / Custom", "custom", "", "", "" },
};

const int kProviderCount = static_cast<int>( sizeof( kProviders ) / sizeof( kProviders[0] ) );

std::string MenuSafeUrl( const std::string& url )
{
    // FLTK 菜单用 '/' 表示子菜单；显示 URL 时换成全角斜杠，避免被拆成子菜单。
    std::string text;
    text.reserve( url.size() + 8 );
    for ( char c : url )
    {
        if ( c == '/' )
        {
            text += "\xEF\xBC\x8F";  // 全角斜杠，避免 FLTK 菜单把 URL 拆成子菜单
        }
        else
        {
            text += c;
        }
    }
    return text;
}

std::vector<std::string> ProviderBaseUrls( int provider_index )
{
    if ( provider_index < 0 || provider_index >= kProviderCount )
    {
        provider_index = 0;
    }
    const std::string id = kProviders[provider_index].id;
    std::vector<std::string> urls;
    if ( id == "glm" )
    {
        urls.push_back( "https://open.bigmodel.cn/api/paas/v4/" );
    }
    else if ( id == "kimi" )
    {
        // 第一个：Kimi Code 编码接口；第二个：月之暗面普通 API。
        urls.push_back( "https://api.kimi.com/coding/v1" );
        urls.push_back( "https://api.moonshot.cn/v1" );
    }
    else if ( kProviders[provider_index].base_url
              && kProviders[provider_index].base_url[0] )
    {
        urls.push_back( kProviders[provider_index].base_url );
    }
    return urls;
}

std::vector<std::string> ProviderBaseLabels( int provider_index )
{
    const std::vector<std::string> urls = ProviderBaseUrls( provider_index );
    const std::string id = ( provider_index >= 0 && provider_index < kProviderCount )
                               ? kProviders[provider_index].id : "";
    std::vector<std::string> labels;
    for ( size_t i = 0; i < urls.size(); ++i )
    {
        std::string prefix;
        if ( id == "kimi" )
        {
            prefix = ( i == 0 ) ? "Kimi Code（默认）: " : "月之暗面 API: ";
        }
        else if ( id == "glm" )
        {
            prefix = "GLM（默认）: ";
        }
        else if ( id == "deepseek" )
        {
            prefix = "DeepSeek（默认）: ";
        }
        else if ( id == "openai" )
        {
            prefix = "OpenAI（默认）: ";
        }
        labels.push_back( prefix + MenuSafeUrl( urls[i] ) );
    }
    return labels;
}

AIDesignWindow* g_AIDesignWindow = nullptr;

std::wstring Widen( const std::string& value )
{
    if ( value.empty() )
    {
        return std::wstring();
    }
    const int needed = MultiByteToWideChar( CP_UTF8, 0, value.c_str(), -1, nullptr, 0 );
    if ( needed <= 0 )
    {
        return std::wstring();
    }
    std::wstring result( static_cast<size_t>( needed ), L'\0' );
    MultiByteToWideChar( CP_UTF8, 0, value.c_str(), -1, &result[0], needed );
    if ( !result.empty() && result.back() == L'\0' )
    {
        result.pop_back();
    }
    return result;
}

std::string Narrow( const std::wstring& value )
{
    if ( value.empty() )
    {
        return std::string();
    }
    const int needed = WideCharToMultiByte( CP_UTF8, 0, value.c_str(), -1, nullptr, 0,
                                            nullptr, nullptr );
    if ( needed <= 0 )
    {
        return std::string();
    }
    std::string result( static_cast<size_t>( needed ), '\0' );
    WideCharToMultiByte( CP_UTF8, 0, value.c_str(), -1, &result[0], needed, nullptr, nullptr );
    if ( !result.empty() && result.back() == '\0' )
    {
        result.pop_back();
    }
    return result;
}

std::string GetEnvUtf8( const char* name )
{
    const DWORD needed = GetEnvironmentVariableW( Widen( name ).c_str(), nullptr, 0 );
    if ( needed == 0 )
    {
        return std::string();
    }
    std::vector<wchar_t> buffer( static_cast<size_t>( needed ) + 1, L'\0' );
    GetEnvironmentVariableW( Widen( name ).c_str(), buffer.data(),
                             static_cast<DWORD>( buffer.size() ) );
    return Narrow( buffer.data() );
}

bool FileExists( const std::string& path )
{
    if ( path.empty() )
    {
        return false;
    }
    const DWORD attr = GetFileAttributesW( Widen( path ).c_str() );
    return attr != INVALID_FILE_ATTRIBUTES && !( attr & FILE_ATTRIBUTE_DIRECTORY );
}

void EnsureDirectory( const std::string& path )
{
    if ( path.empty() )
    {
        return;
    }
    const std::wstring wide = Widen( path );
    std::wstring current;
    for ( size_t i = 0; i < wide.size(); ++i )
    {
        current.push_back( wide[i] );
        if ( wide[i] == L'\\' || wide[i] == L'/' || i + 1 == wide.size() )
        {
            if ( current.size() > 3 )
            {
                CreateDirectoryW( current.c_str(), nullptr );
            }
        }
    }
}

std::string JoinPath( const std::string& a, const std::string& b )
{
    if ( a.empty() )
    {
        return b;
    }
    if ( b.empty() )
    {
        return a;
    }
    const char last = a.back();
    if ( last == '\\' || last == '/' )
    {
        return a + b;
    }
    return a + "\\" + b;
}

std::string Trim( const std::string& value )
{
    const size_t begin = value.find_first_not_of( " \t\r\n" );
    if ( begin == std::string::npos )
    {
        return std::string();
    }
    const size_t end = value.find_last_not_of( " \t\r\n" );
    return value.substr( begin, end - begin + 1 );
}

std::string BaseName( const std::string& path )
{
    const size_t slash = path.find_last_of( "\\/" );
    return slash == std::string::npos ? path : path.substr( slash + 1 );
}

std::string ExtensionLower( const std::string& path )
{
    const size_t dot = path.find_last_of( '.' );
    if ( dot == std::string::npos )
    {
        return std::string();
    }
    std::string extension = path.substr( dot );
    std::transform( extension.begin(), extension.end(), extension.begin(),
                    []( unsigned char c ) { return static_cast<char>( std::tolower( c ) ); } );
    return extension;
}

bool IsSupportedAttachment( const std::string& path )
{
    const std::string extension = ExtensionLower( path );
    return extension == ".docx" || extension == ".pdf" || extension == ".xlsx"
        || extension == ".xls" || extension == ".txt" || extension == ".md"
        || extension == ".csv" || extension == ".json" || extension == ".log";
}

std::vector<std::string> SplitComma( const std::string& value )
{
    std::vector<std::string> parts;
    size_t start = 0;
    while ( start <= value.size() )
    {
        const size_t comma = value.find( ',', start );
        const std::string part = Trim( value.substr( start, comma - start ) );
        if ( !part.empty() )
        {
            parts.push_back( part );
        }
        if ( comma == std::string::npos )
        {
            break;
        }
        start = comma + 1;
    }
    return parts;
}

std::string Base64Encode( const std::vector<unsigned char>& data )
{
    static const char table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    int value = 0;
    int bits = -6;
    for ( unsigned char c : data )
    {
        value = ( value << 8 ) + c;
        bits += 8;
        while ( bits >= 0 )
        {
            out.push_back( table[( value >> bits ) & 0x3F] );
            bits -= 6;
        }
    }
    if ( bits > -6 )
    {
        out.push_back( table[( ( value << 8 ) >> ( bits + 8 ) ) & 0x3F] );
    }
    while ( out.size() % 4 )
    {
        out.push_back( '=' );
    }
    return out;
}

std::vector<unsigned char> Base64Decode( const std::string& input )
{
    static const std::string table =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::vector<unsigned char> out;
    int value = 0;
    int bits = -8;
    for ( unsigned char c : input )
    {
        if ( c == '=' )
        {
            break;
        }
        const size_t pos = table.find( static_cast<char>( c ) );
        if ( pos == std::string::npos )
        {
            continue;
        }
        value = ( value << 6 ) + static_cast<int>( pos );
        bits += 6;
        if ( bits >= 0 )
        {
            out.push_back( static_cast<unsigned char>( ( value >> bits ) & 0xFF ) );
            bits -= 8;
        }
    }
    return out;
}

std::string ProtectSecret( const std::string& plain )
{
    if ( plain.empty() )
    {
        return std::string();
    }
    DATA_BLOB input;
    input.pbData = reinterpret_cast<BYTE*>( const_cast<char*>( plain.data() ) );
    input.cbData = static_cast<DWORD>( plain.size() );
    DATA_BLOB output;
    std::memset( &output, 0, sizeof( output ) );
    if ( CryptProtectData( &input, L"OpenVSP-R2 AI API Key", nullptr, nullptr, nullptr,
                           CRYPTPROTECT_UI_FORBIDDEN, &output ) )
    {
        std::vector<unsigned char> bytes( output.pbData, output.pbData + output.cbData );
        LocalFree( output.pbData );
        return "dpapi:" + Base64Encode( bytes );
    }
    return std::string();
}

std::string UnprotectSecret( const std::string& stored )
{
    if ( stored.rfind( "dpapi:", 0 ) != 0 )
    {
        return stored;
    }
    const std::vector<unsigned char> bytes = Base64Decode( stored.substr( 6 ) );
    if ( bytes.empty() )
    {
        return std::string();
    }
    DATA_BLOB input;
    input.pbData = const_cast<BYTE*>( bytes.data() );
    input.cbData = static_cast<DWORD>( bytes.size() );
    DATA_BLOB output;
    std::memset( &output, 0, sizeof( output ) );
    std::string plain;
    if ( CryptUnprotectData( &input, nullptr, nullptr, nullptr, nullptr,
                             CRYPTPROTECT_UI_FORBIDDEN, &output ) )
    {
        plain.assign( reinterpret_cast<char*>( output.pbData ), output.cbData );
        LocalFree( output.pbData );
    }
    return plain;
}

std::string JsonEscape( const std::string& value )
{
    std::string out;
    out.reserve( value.size() + 8 );
    for ( char c : value )
    {
        switch ( c )
        {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if ( static_cast<unsigned char>( c ) < 0x20 )
            {
                char buffer[8];
                std::snprintf( buffer, sizeof( buffer ), "\\u%04x", c );
                out += buffer;
            }
            else
            {
                out.push_back( c );
            }
            break;
        }
    }
    return out;
}

std::string ExtractJsonString( const std::string& json, const std::string& key )
{
    const std::string pattern = "\"" + key + "\"";
    size_t pos = json.find( pattern );
    if ( pos == std::string::npos )
    {
        return std::string();
    }
    pos += pattern.size();
    while ( pos < json.size() && ( json[pos] == ' ' || json[pos] == '\t' ||
                                   json[pos] == '\r' || json[pos] == '\n' ) )
    {
        ++pos;
    }
    if ( pos >= json.size() || json[pos] != ':' )
    {
        return std::string();
    }
    ++pos;
    while ( pos < json.size() && ( json[pos] == ' ' || json[pos] == '\t' ||
                                   json[pos] == '\r' || json[pos] == '\n' ) )
    {
        ++pos;
    }
    if ( pos >= json.size() || json[pos] != '"' )
    {
        return std::string();
    }
    ++pos;
    std::string result;
    bool escaped = false;
    for ( ; pos < json.size(); ++pos )
    {
        const char c = json[pos];
        if ( escaped )
        {
            switch ( c )
            {
            case 'n': result.push_back( '\n' ); break;
            case 'r': result.push_back( '\r' ); break;
            case 't': result.push_back( '\t' ); break;
            default: result.push_back( c ); break;
            }
            escaped = false;
        }
        else if ( c == '\\' )
        {
            escaped = true;
        }
        else if ( c == '"' )
        {
            return result;
        }
        else
        {
            result.push_back( c );
        }
    }
    return std::string();
}

std::string ExtractJsonBool( const std::string& json, const std::string& key )
{
    const std::string pattern = "\"" + key + "\"";
    size_t pos = json.find( pattern );
    if ( pos == std::string::npos )
    {
        return std::string();
    }
    pos += pattern.size();
    while ( pos < json.size() && ( json[pos] == ' ' || json[pos] == '\t' ||
                                   json[pos] == '\r' || json[pos] == '\n' ) )
    {
        ++pos;
    }
    if ( pos >= json.size() || json[pos] != ':' )
    {
        return std::string();
    }
    ++pos;
    while ( pos < json.size() && ( json[pos] == ' ' || json[pos] == '\t' ||
                                   json[pos] == '\r' || json[pos] == '\n' ) )
    {
        ++pos;
    }
    if ( json.compare( pos, 4, "true" ) == 0 )
    {
        return "true";
    }
    if ( json.compare( pos, 5, "false" ) == 0 )
    {
        return "false";
    }
    if ( pos < json.size() && json[pos] == '"' )
    {
        return ExtractJsonString( json, key );
    }
    return std::string();
}

std::vector<std::string> ExtractModelIds( const std::string& body )
{
    std::vector<std::string> ids;
    size_t pos = 0;
    while ( ( pos = body.find( "\"id\"", pos ) ) != std::string::npos )
    {
        pos += 4;
        while ( pos < body.size() &&
                ( body[pos] == ' ' || body[pos] == '\t' || body[pos] == '\r' ||
                  body[pos] == '\n' ) )
        {
            ++pos;
        }
        if ( pos >= body.size() || body[pos] != ':' )
        {
            continue;
        }
        ++pos;
        while ( pos < body.size() &&
                ( body[pos] == ' ' || body[pos] == '\t' || body[pos] == '\r' ||
                  body[pos] == '\n' ) )
        {
            ++pos;
        }
        if ( pos >= body.size() || body[pos] != '"' )
        {
            continue;
        }
        ++pos;
        std::string id;
        while ( pos < body.size() && body[pos] != '"' )
        {
            if ( body[pos] == '\\' && pos + 1 < body.size() )
            {
                ++pos;
            }
            id.push_back( body[pos] );
            ++pos;
        }
        id = Trim( id );
        if ( !id.empty() )
        {
            ids.push_back( id );
        }
        ++pos;
    }
    std::sort( ids.begin(), ids.end() );
    ids.erase( std::unique( ids.begin(), ids.end() ), ids.end() );
    return ids;
}

std::string SettingsPath()
{
    std::string base = GetEnvUtf8( "LOCALAPPDATA" );
    if ( base.empty() )
    {
        base = GetEnvUtf8( "USERPROFILE" );
    }
    if ( base.empty() )
    {
        return std::string();
    }
    const std::string dir = JoinPath( base, "OpenVSP-R2" );
    EnsureDirectory( dir );
    return JoinPath( dir, "ai_ui.cfg" );
}

std::string DocumentsDir()
{
    std::string base = GetEnvUtf8( "USERPROFILE" );
    if ( base.empty() )
    {
        base = GetEnvUtf8( "LOCALAPPDATA" );
    }
    return JoinPath( base, "Documents" );
}

bool HttpGetText( const std::string& url, const std::string& bearer_token,
                  std::string& body, std::string& error )
{
    body.clear();
    error.clear();

    const std::wstring wide_url = Widen( url );
    URL_COMPONENTS components;
    std::memset( &components, 0, sizeof( components ) );
    components.dwStructSize = sizeof( components );
    components.dwSchemeLength = static_cast<DWORD>( -1 );
    components.dwHostNameLength = static_cast<DWORD>( -1 );
    components.dwUrlPathLength = static_cast<DWORD>( -1 );
    components.dwExtraInfoLength = static_cast<DWORD>( -1 );

    if ( !WinHttpCrackUrl( wide_url.c_str(), 0, 0, &components ) )
    {
        error = "请求地址格式无效，请填写完整 URL（含 https://）。";
        return false;
    }

    const std::wstring host( components.lpszHostName, components.dwHostNameLength );
    std::wstring path( components.lpszUrlPath, components.dwUrlPathLength );
    if ( components.lpszExtraInfo && components.dwExtraInfoLength > 0 )
    {
        path.append( components.lpszExtraInfo, components.dwExtraInfoLength );
    }
    if ( path.empty() )
    {
        path = L"/";
    }

    HINTERNET session = WinHttpOpen( L"OpenVSP-R2-AI/1.0",
                                     WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                     WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0 );
    if ( !session )
    {
        error = "无法初始化网络会话（WinHTTP）。";
        return false;
    }
    WinHttpSetTimeouts( session, 8000, 8000, 15000, 30000 );

    HINTERNET connect = WinHttpConnect( session, host.c_str(), components.nPort, 0 );
    if ( !connect )
    {
        WinHttpCloseHandle( session );
        error = "无法连接到服务器，请检查网络/代理设置。";
        return false;
    }

    const DWORD flags = ( components.nScheme == INTERNET_SCHEME_HTTPS ) ? WINHTTP_FLAG_SECURE : 0;
    HINTERNET request = WinHttpOpenRequest( connect, L"GET", path.c_str(), nullptr,
                                            WINHTTP_NO_REFERER,
                                            WINHTTP_DEFAULT_ACCEPT_TYPES, flags );
    if ( !request )
    {
        WinHttpCloseHandle( connect );
        WinHttpCloseHandle( session );
        error = "无法创建请求。";
        return false;
    }

    std::wstring headers = L"Accept: application/json\r\nUser-Agent: OpenVSP-R2-AI/1.0\r\n";
    if ( !bearer_token.empty() )
    {
        headers += L"Authorization: Bearer " + Widen( bearer_token ) + L"\r\n";
    }

    BOOL ok = WinHttpSendRequest( request, headers.c_str(),
                                  static_cast<DWORD>( -1 ), WINHTTP_NO_REQUEST_DATA, 0, 0, 0 );
    if ( ok )
    {
        ok = WinHttpReceiveResponse( request, nullptr );
    }

    DWORD status = 0;
    DWORD status_size = sizeof( status );
    if ( ok )
    {
        WinHttpQueryHeaders( request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
                             WINHTTP_NO_HEADER_INDEX );
    }

    if ( !ok )
    {
        const DWORD last = GetLastError();
        std::ostringstream stream;
        stream << "网络请求失败（错误码 " << last << "）。";
        error = stream.str();
    }
    else if ( status != 200 )
    {
        std::ostringstream stream;
        stream << "服务器返回 HTTP " << status << "，请检查 API Key、地址或权限。";
        error = stream.str();
    }
    else
    {
        DWORD available = 0;
        do
        {
            available = 0;
            if ( !WinHttpQueryDataAvailable( request, &available ) )
            {
                break;
            }
            if ( available == 0 )
            {
                break;
            }
            std::vector<char> buffer( available + 1, '\0' );
            DWORD received = 0;
            if ( !WinHttpReadData( request, buffer.data(), available, &received ) )
            {
                break;
            }
            body.append( buffer.data(), received );
        }
        while ( available > 0 );
    }

    WinHttpCloseHandle( request );
    WinHttpCloseHandle( connect );
    WinHttpCloseHandle( session );
    return error.empty();
}

std::function<bool( const std::string& )> g_ImportModelHandler;

std::string FindNewestVsp3( const std::string& directory )
{
    if ( directory.empty() )
    {
        return std::string();
    }
    WIN32_FIND_DATAW data;
    const std::wstring pattern = Widen( directory + "\\*.vsp3" );
    HANDLE find = FindFirstFileW( pattern.c_str(), &data );
    if ( find == INVALID_HANDLE_VALUE )
    {
        return std::string();
    }
    std::string best;
    ULONGLONG best_time = 0;
    do
    {
        if ( data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY )
        {
            continue;
        }
        std::wstring full = Widen( directory + "\\" ) + data.cFileName;
        const std::string path = Narrow( full );
        ULONGLONG stamp = ( static_cast<ULONGLONG>( data.ftLastWriteTime.dwHighDateTime ) << 32 )
                          | data.ftLastWriteTime.dwLowDateTime;
        if ( stamp >= best_time )
        {
            best_time = stamp;
            best = path;
        }
    }
    while ( FindNextFileW( find, &data ) );
    FindClose( find );
    return best;
}

bool HttpPostJson( const std::string& url, const std::string& bearer_token,
                   const std::string& payload, std::string& body, std::string& error )
{
    body.clear();
    error.clear();

    const std::wstring wide_url = Widen( url );
    URL_COMPONENTS components;
    std::memset( &components, 0, sizeof( components ) );
    components.dwStructSize = sizeof( components );
    components.dwSchemeLength = static_cast<DWORD>( -1 );
    components.dwHostNameLength = static_cast<DWORD>( -1 );
    components.dwUrlPathLength = static_cast<DWORD>( -1 );
    components.dwExtraInfoLength = static_cast<DWORD>( -1 );

    if ( !WinHttpCrackUrl( wide_url.c_str(), 0, 0, &components ) )
    {
        error = "请求地址格式无效，请填写完整 URL（含 https://）。";
        return false;
    }

    const std::wstring host( components.lpszHostName, components.dwHostNameLength );
    std::wstring path( components.lpszUrlPath, components.dwUrlPathLength );
    if ( components.lpszExtraInfo && components.dwExtraInfoLength > 0 )
    {
        path.append( components.lpszExtraInfo, components.dwExtraInfoLength );
    }
    if ( path.empty() )
    {
        path = L"/";
    }

    HINTERNET session = WinHttpOpen( L"OpenVSP-R2-AI/1.0",
                                     WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                     WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0 );
    if ( !session )
    {
        error = "无法初始化网络会话（WinHTTP）。";
        return false;
    }
    WinHttpSetTimeouts( session, 8000, 8000, 15000, 30000 );

    HINTERNET connect = WinHttpConnect( session, host.c_str(), components.nPort, 0 );
    if ( !connect )
    {
        WinHttpCloseHandle( session );
        error = "无法连接到服务器，请检查网络/代理设置。";
        return false;
    }

    const DWORD flags = ( components.nScheme == INTERNET_SCHEME_HTTPS ) ? WINHTTP_FLAG_SECURE : 0;
    HINTERNET request = WinHttpOpenRequest( connect, L"POST", path.c_str(), nullptr,
                                            WINHTTP_NO_REFERER,
                                            WINHTTP_DEFAULT_ACCEPT_TYPES, flags );
    if ( !request )
    {
        WinHttpCloseHandle( connect );
        WinHttpCloseHandle( session );
        error = "无法创建请求。";
        return false;
    }

    std::wstring headers = L"Content-Type: application/json\r\nAccept: application/json\r\nUser-Agent: OpenVSP-R2-AI/1.0\r\n";
    if ( !bearer_token.empty() )
    {
        headers += L"Authorization: Bearer " + Widen( bearer_token ) + L"\r\n";
    }

    BOOL ok = WinHttpSendRequest( request, headers.c_str(),
                                  static_cast<DWORD>( -1 ), reinterpret_cast<LPVOID>( const_cast<char*>( payload.data() ) ),
                                  static_cast<DWORD>( payload.size() ),
                                  static_cast<DWORD>( payload.size() ), 0 );
    if ( ok )
    {
        ok = WinHttpReceiveResponse( request, nullptr );
    }

    DWORD status = 0;
    DWORD status_size = sizeof( status );
    if ( ok )
    {
        WinHttpQueryHeaders( request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
                             WINHTTP_NO_HEADER_INDEX );
    }

    if ( !ok )
    {
        const DWORD last = GetLastError();
        std::ostringstream stream;
        stream << "网络请求失败（错误码 " << last << "）。";
        error = stream.str();
    }
    else if ( status != 200 )
    {
        std::ostringstream stream;
        stream << "服务器返回 HTTP " << status << "，请检查 API Key、地址或权限。";
        error = stream.str();
    }
    else
    {
        DWORD available = 0;
        do
        {
            available = 0;
            if ( !WinHttpQueryDataAvailable( request, &available ) )
            {
                break;
            }
            if ( available == 0 )
            {
                break;
            }
            std::vector<char> buffer( available + 1, '\0' );
            DWORD received = 0;
            if ( !WinHttpReadData( request, buffer.data(), available, &received ) )
            {
                break;
            }
            body.append( buffer.data(), received );
        }
        while ( available > 0 );
    }

    WinHttpCloseHandle( request );
    WinHttpCloseHandle( connect );
    WinHttpCloseHandle( session );
    return error.empty();
}

struct LogMessage
{
    std::string text;
};

struct VerifyMessage
{
    bool ok;
    std::string text;
};

struct StatusMessage
{
    std::string text;
    bool error;
};

struct FinishedMessage
{
    int code;
};

// ---------------------------------------------------------------------------
// MCP 调用提示词弹窗（只保留可复制给 Agent / Harness 的提示词）
// ---------------------------------------------------------------------------
Fl_Window* g_MCPWindow = nullptr;
Fl_Text_Buffer* g_MCPBuffer = nullptr;
Fl_Text_Display* g_MCPDisplay = nullptr;
std::string g_MCPText;

std::string FullPathOf( const std::string& path )
{
    const std::wstring wide = Widen( path );
    if ( wide.empty() )
    {
        return path;
    }
    wchar_t buffer[32768];
    const DWORD n = GetFullPathNameW( wide.c_str(), 32768, buffer, nullptr );
    if ( n == 0 || n >= 32768 )
    {
        return path;
    }
    return Narrow( std::wstring( buffer, n ) );
}

void MCPCopyCB( Fl_Widget*, void* )
{
    if ( g_MCPBuffer )
    {
        Fl::copy( g_MCPBuffer->text(), g_MCPBuffer->length(), 1 );
    }
}

void MCPCloseCB( Fl_Widget* w, void* )
{
    if ( w && w->window() )
    {
        w->window()->hide();
    }
}

void MCPRefreshWindow()
{
    if ( g_MCPBuffer )
    {
        g_MCPBuffer->text( g_MCPText.c_str() );
    }
}

}  // namespace

void AIDesignWindow::MCPTutorialCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->ShowMCPTutorial();
    }
}

void AIDesignWindow::ShowMCPTutorial()
{
    const std::string ai_dir = FullPathOf( ResolveAIDir() );
    const std::string mcp_dir = FullPathOf( JoinPath( ai_dir, "..\\mcp" ) );
    const std::string app_root = FullPathOf( JoinPath( ai_dir, ".." ) );
    const std::string python = ResolvePython();
    const std::string server = JoinPath( mcp_dir, "openvsp_mcp_server.py" );
    const std::string installer = JoinPath( mcp_dir, "install_mcp.py" );

    std::ostringstream prompt;
    prompt << "你是 OpenVSP 飞行器设计代理。请使用本机 OpenVSP AI R2 的 MCP 服务器完成设计、分析和报告任务。\n\n"
           << "【任务要求】\n"
           << "1. 通过 MCP 工具建立或读取 OpenVSP 模型；\n"
           << "2. 运行 VSPAERO 气动与稳定性分析；\n"
           << "3. 检查俯仰和航向静稳定性；若不稳定，按布局调整参数并复算，最多 3 轮；\n"
           << "4. 使用最终模型和实际运行数据生成 Word 报告；\n"
           << "5. 明确区分任务完成、工程可行、证据完整和推荐状态；不得伪造 pass 或把概念分析称为已验证可行。\n\n"
           << "【MCP 服务器：stdio】\n"
           << "command: \"" << python << "\"\n"
           << "args: [\"" << server << "\"]\n"
           << "env: OPENVSP_ROOT=\"" << app_root << "\"\n\n"
           << "可用工具以 MCP tools/list 为准，主要包括：\n"
           << "openvsp_new_model, openvsp_build_aircraft, openvsp_get_geometry,\n"
           << "openvsp_modify_parameter, openvsp_run_vspaero, openvsp_export,\n"
           << "openvsp_generate_report, openvsp_list_files。\n\n"
           << "如果当前 Agent/Harness 尚未注册该 MCP 服务器，请先执行：\n"
           << "\"" << python << "\" \"" << installer << "\" --install --target all\n"
           << "或把上面的 stdio command/args/env 写入宿主 MCP 配置。\n";
    g_MCPText = prompt.str();

    if ( !g_MCPWindow )
    {
        Fl_Window* win = new Fl_Window( 920, 650, "OpenVSP AI R2 MCP 调用提示词" );
        win->begin();
        Fl_Box* title = new Fl_Box( 15, 8, 890, 28,
            "MCP 调用提示词 — 复制后粘贴给 Agent / Harness" );
        title->labelfont( FL_BOLD );
        title->labelsize( 14 );
        g_MCPBuffer = new Fl_Text_Buffer();
        g_MCPDisplay = new Fl_Text_Display( 15, 44, 890, 530 );
        g_MCPDisplay->buffer( g_MCPBuffer );
        g_MCPDisplay->wrap_mode( Fl_Text_Display::WRAP_AT_BOUNDS, 0 );
        Fl_Button* copy_btn = new Fl_Button( 15, 590, 170, 32, "复制提示词" );
        copy_btn->callback( MCPCopyCB, nullptr );
        copy_btn->tooltip( "复制整段提示词，粘贴到 Agent / Harness 的输入框" );
        Fl_Button* close_btn = new Fl_Button( 805, 590, 100, 32, "关闭" );
        close_btn->callback( MCPCloseCB, nullptr );
        win->end();
        win->set_modal();
        g_MCPWindow = win;
    }
    MCPRefreshWindow();
    g_MCPWindow->show();
    if ( g_MCPDisplay )
    {
        g_MCPDisplay->scroll( 0, 0 );
    }
    g_MCPWindow->redraw();
}

void AIDesignWindow::SetImportModelHandler( std::function<bool( const std::string& )> handler )
{
    g_ImportModelHandler = std::move( handler );
}

AIDesignWindow* AIDesignWindow::GetInstance()
{
    if ( !g_AIDesignWindow )
    {
        g_AIDesignWindow = new AIDesignWindow();
    }
    return g_AIDesignWindow;
}

void AIDesignWindow::ShowWindow()
{
    AIDesignWindow* window = GetInstance();
    window->show();
    window->Fl_Window::redraw();
}

AIDesignWindow::AIDesignWindow() : Fl_Window( 940, 660, "OpenVSP AI R2 · AI 飞行器设计助手" )
{
    BuildWidgets();
    LoadSettings();

    if ( m_ApiKeyInput && m_ApiKeyInput->value() && m_ApiKeyInput->value()[0] )
    {
        SetStatus( "已加载本机保存的 API Key，正在验证...", false );
        StartVerifyKey();
    }

    // Optional preload hook for demos/automation: OPENVSP_AI_ATTACHMENTS uses
    // ';' separated document paths, e.g. Word/PDF/Excel technical documents.
    const std::string preload = GetEnvUtf8( "OPENVSP_AI_ATTACHMENTS" );
    if ( !preload.empty() )
    {
        std::string paths = preload;
        size_t start = 0;
        while ( start <= paths.size() )
        {
            const size_t separator = paths.find( ';', start );
            const std::string path = Trim( paths.substr( start, separator - start ) );
            if ( !path.empty() && FileExists( path ) && IsSupportedAttachment( path ) )
            {
                m_Attachments.push_back( path );
            }
            if ( separator == std::string::npos )
            {
                break;
            }
            start = separator + 1;
        }
        RefreshAttachmentList();
    }

    const std::string prompt_preload = GetEnvUtf8( "OPENVSP_AI_PROMPT" );
    if ( !prompt_preload.empty() && m_PromptInput )
    {
        m_PromptInput->value( prompt_preload.c_str() );
        m_PromptInput->position( 0 );
    }

    const std::string case_preload = GetEnvUtf8( "OPENVSP_AI_CASE_MODEL" );
    if ( !case_preload.empty() && FileExists( case_preload )
         && ExtensionLower( case_preload ) == ".vsp3" )
    {
        m_CasePath = case_preload;
        RefreshCaseList();
    }

    end();
    resizable( this );
}

AIDesignWindow::~AIDesignWindow()
{
    StoreCurrentConfig( false );
    SaveSettings();
    m_StopRequested = true;
    if ( m_ProcessHandle )
    {
        TerminateProcess( static_cast<HANDLE>( m_ProcessHandle ), 1 );
    }
    if ( m_ReaderThread.joinable() )
    {
        m_ReaderThread.join();
    }
    if ( m_VerifyThread.joinable() )
    {
        m_VerifyThread.join();
    }
    g_AIDesignWindow = nullptr;
}

void AIDesignWindow::BuildWidgets()
{
    begin();

    Fl_Box* title = new Fl_Box( 15, 8, 910, 24,
                                "AI 飞行器设计与优化（OpenAI 兼容 API / 内置设计报告模板）" );
    title->labelfont( FL_HELVETICA_BOLD );
    title->labelsize( 14 );

    // Provider row
    Fl_Box* provider_label = new Fl_Box( 15, 44, 80, 24, "服务商:" );
    provider_label->align( FL_ALIGN_RIGHT | FL_ALIGN_INSIDE );
    m_ProviderChoice = new Fl_Choice( 100, 44, 190, 24 );
    for ( int i = 0; i < kProviderCount; ++i )
    {
        m_ProviderChoice->add( kProviders[i].label );
    }
    m_ProviderChoice->value( 0 );
    m_ProviderChoice->callback( ProviderCB, this );

    Fl_Box* url_label = new Fl_Box( 300, 44, 70, 24, "请求地址:" );
    url_label->align( FL_ALIGN_RIGHT | FL_ALIGN_INSIDE );
    m_BasePresetChoice = new Fl_Choice( 375, 44, 245, 24 );
    m_BasePresetChoice->callback( BasePresetCB, this );
    m_BasePresetChoice->tooltip( "按服务商选择请求地址；最后一项“自定义…”可手动输入" );
    m_BaseUrlInput = new Fl_Input( 625, 44, 295, 24 );
    m_BaseUrlInput->callback( BaseUrlInputCB, this );
    m_BaseUrlInput->tooltip( "实际请求地址（base_url）；选择预设后可继续编辑，最后一项为自定义" );
    UpdateBaseUrlChoices( 0, "" );

    // API key / model row
    Fl_Box* key_label = new Fl_Box( 15, 78, 80, 24, "API Key:" );
    key_label->align( FL_ALIGN_RIGHT | FL_ALIGN_INSIDE );
    m_ApiKeyInput = new Fl_Input( 100, 78, 300, 24 );
    m_ApiKeyInput->type( FL_SECRET_INPUT );

    m_SaveKeyButton = new Fl_Button( 405, 78, 75, 24, "保存" );
    m_SaveKeyButton->callback( SaveKeyCB, this );
    m_SaveKeyButton->tooltip( "将当前 API Key 经 Windows DPAPI 加密保存到本机，"
                              "并按服务商分别记忆；不会上传到任何第三方" );

    m_FetchButton = new Fl_Button( 490, 78, 110, 24, "获取模型列表" );
    m_FetchButton->callback( FetchCB, this );

    Fl_Box* model_label = new Fl_Box( 610, 78, 50, 24, "模型:" );
    model_label->align( FL_ALIGN_RIGHT | FL_ALIGN_INSIDE );
    m_ModelChoice = new Fl_Input_Choice( 665, 78, 260, 24 );
    m_ModelChoice->callback( ModelCB, this );

    // Prompt
    Fl_Box* prompt_label = new Fl_Box( 15, 108, 910, 20,
                                       "设计需求（自然语言：重量、海拔、速度、布局、发动机、翼型/尾翼形式等）:" );
    prompt_label->align( FL_ALIGN_LEFT | FL_ALIGN_INSIDE );
    m_PromptInput = new Fl_Multiline_Input( 15, 130, 910, 90 );
    m_PromptInput->value(
        "设计一架常规布局电动长航时无人机，最大起飞重量 25 kg，海拔 1000 m，"
        "巡航速度 25 m/s，失速速度不超过 12 m/s，有效载荷 5 kg；"
        "翼展不超过 4.5 m，展弦比不小于 9；采用常规尾翼（水平尾翼 + 垂直尾翼）"
        "和电动动力，翼型根据巡航工况从翼型库自动选择。请生成 OpenVSP 模型，"
        "完成 VSPAERO 气动计算、CLmax、巡航升阻比、静稳定裕度、功率需求与续航估算，"
        "并生成设计报告。" );

    // Upload row: technical documents are parsed by the Python backend and merged
    // into the design requirements.
    m_UploadButton = new Fl_Button( 15, 226, 125, 26, "上传文档…" );
    m_UploadButton->callback( UploadCB, this );
    m_UploadButton->tooltip( "上传 Word/PDF/Excel/TXT/CSV/JSON 等技术资料，"
                             "AI 解析其中的技术参数、设计输入和设备选型后与设计需求合并" );
    m_AttachmentOutput = new Fl_Output( 150, 226, 605, 26 );
    m_AttachmentOutput->value( "未上传文档（可选：Word/PDF/Excel/TXT 等技术资料）" );
    m_AttachmentOutput->tooltip( "选中的文档路径；解析由 AI 后端完成" );
    m_ClearAttachmentsButton = new Fl_Button( 765, 226, 160, 26, "清空附件" );
    m_ClearAttachmentsButton->callback( ClearCB, this );
    m_ClearAttachmentsButton->deactivate();

    // Uploaded .vsp3 case acts as the baseline for "modify / optimize".
    m_UploadCaseButton = new Fl_Button( 15, 258, 125, 26, "上传案例…" );
    m_UploadCaseButton->callback( UploadCaseCB, this );
    m_UploadCaseButton->tooltip( "上传 .vsp3 案例，AI 将基于该案例按设计需求进行修改或优化" );
    m_CaseOutput = new Fl_Output( 150, 258, 605, 26 );
    m_CaseOutput->value( "未上传案例（可选：.vsp3；作为修改/优化基线）" );
    m_CaseOutput->tooltip( "上传的 .vsp3 案例路径；将作为设计/GA 优化的基线" );
    m_ClearCaseButton = new Fl_Button( 765, 258, 160, 26, "清空案例" );
    m_ClearCaseButton->callback( ClearCaseCB, this );
    m_ClearCaseButton->deactivate();

    m_StatusBox = new Fl_Box( 15, 290, 910, 22, "就绪。填写 API Key 后可获取模型列表；留空则使用离线内置设计模式。" );
    m_StatusBox->box( FL_ENGRAVED_BOX );
    m_StatusBox->align( FL_ALIGN_LEFT | FL_ALIGN_INSIDE );
    m_StatusBox->labelsize( 11 );
    m_StatusBox->labelcolor( FL_DARK_GREEN );

    // Log
    Fl_Box* log_label = new Fl_Box( 15, 318, 910, 20, "运行日志 / 设计过程:" );
    log_label->align( FL_ALIGN_LEFT | FL_ALIGN_INSIDE );
    m_LogBuffer = new Fl_Text_Buffer();
    m_LogDisplay = new Fl_Text_Display( 15, 340, 910, 184 );
    m_LogDisplay->buffer( m_LogBuffer );
    m_LogDisplay->wrap_mode( Fl_Text_Display::WRAP_AT_BOUNDS, 0 );
    m_LogDisplay->textfont( FL_COURIER );
    m_LogDisplay->textsize( 11 );

    // Action row: Start | Genetic optimization | Import model | Stop | Report | Offline | Close
    m_StartButton = new Fl_Return_Button( 15, 536, 110, 28, "开始设计" );
    m_StartButton->callback( StartCB, this );

    m_GAButton = new Fl_Button( 135, 536, 140, 28, "遗传算法优化" );
    m_GAButton->callback( GACB, this );
    m_GAButton->tooltip( "根据当前设计输入与工况，用遗传算法优化几何，输出优化后 vsp3 与 GA 报告" );

    m_ImportModelButton = new Fl_Button( 285, 536, 120, 28, "导入OpenVSP" );
    m_ImportModelButton->callback( ImportModelCB, this );
    m_ImportModelButton->tooltip( "导入OpenVSP：将最近一次 AI 设计/遗传算法优化生成的模型载入当前 OpenVSP 编辑" );
    m_ImportModelButton->deactivate();

    m_StopButton = new Fl_Button( 415, 536, 80, 28, "停止" );
    m_StopButton->callback( StopCB, this );
    m_StopButton->deactivate();

    m_OpenButton = new Fl_Button( 505, 536, 120, 28, "打开设计报告" );
    m_OpenButton->callback( OpenCB, this );
    m_OpenButton->deactivate();

    m_MCPButton = new Fl_Button( 745, 536, 75, 28, "MCP调用" );
    m_MCPButton->callback( MCPTutorialCB, this );
    m_MCPButton->tooltip( "打开 MCP 调用提示词；复制后粘贴给 Agent / Harness" );

    m_OfflineCheck = new Fl_Check_Button( 635, 536, 105, 28, "离线模式" );
    m_OfflineCheck->tooltip( "不调用大模型，使用内置规则设计流程（无需 API Key）" );

    m_CloseButton = new Fl_Button( 825, 536, 100, 28, "关闭" );
    m_CloseButton->callback( CloseCB, this );

    Fl_Box* footer = new Fl_Box( 15, 572, 910, 70,
        "工作流程：读取需求/上传文档 → 调用大模型规划（或离线规则设计）→ 驱动 OpenVSP 建模 → "
        "VSPAERO 气动计算 → 自动优化/遗传算法优化 → 按内置模板生成 Word 设计报告。\n"
        "上传的 Word/PDF/Excel/TXT 等文档由 AI 后端解析，提取技术参数、设计输入与设备选型，"
        "与自然语言需求共同作为设计输入。\n"
        "“开始设计”生成基线方案；“遗传算法优化”在当前方案/工况上做 GA 优化；“导入OpenVSP”把最近一次生成的 vsp3 "
        "导入当前 OpenVSP 编辑。" );
    footer->labelsize( 10 );
    footer->align( FL_ALIGN_LEFT | FL_ALIGN_INSIDE );

    ApplyProvider( 0, true );
    if ( m_PromptInput )
    {
        m_PromptInput->position( 0 );
    }
}

void AIDesignWindow::ProviderCB( Fl_Widget* w, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window && window->m_ProviderChoice )
    {
        window->SwitchProvider( window->m_ProviderChoice->value() );
    }
}

void AIDesignWindow::ApplyProvider( int provider_index, bool reset_base )
{
    if ( provider_index < 0 || provider_index >= kProviderCount )
    {
        provider_index = kProviderCount - 1;
    }
    if ( reset_base )
    {
        UpdateBaseUrlChoices( provider_index, "" );
        m_ModelChoice->clear();
        const std::vector<std::string> models =
            SplitComma( kProviders[provider_index].builtin_models );
        for ( const std::string& model : models )
        {
            m_ModelChoice->add( model.c_str() );
        }
        if ( !models.empty() )
        {
            m_ModelChoice->value( 0 );
        }
    }
}

std::string AIDesignWindow::ResolveAIDir() const
{
    const std::string env_dir = GetEnvUtf8( "OPENVSP_AI_DIR" );
    if ( !env_dir.empty() && FileExists( JoinPath( env_dir, "ai_design_agent.py" ) ) )
    {
        return env_dir;
    }

    std::string exe_dir;
    try
    {
        exe_dir = vsp::l10n::ExecutableDirectory().u8string();
    }
    catch ( ... )
    {
        exe_dir.clear();
    }

    std::vector<std::string> candidates;
    if ( !exe_dir.empty() )
    {
        candidates.push_back( JoinPath( exe_dir, "ai" ) );
        candidates.push_back( JoinPath( exe_dir, "..\\ai" ) );
        candidates.push_back( JoinPath( exe_dir, "..\\..\\ai" ) );
        candidates.push_back( JoinPath( exe_dir, "..\\..\\..\\ai" ) );
        candidates.push_back( JoinPath( exe_dir, "..\\..\\..\\..\\ai" ) );
    }
    const std::string r2_root = GetEnvUtf8( "OPENVSP_R2_ROOT" );
    if ( !r2_root.empty() )
    {
        candidates.push_back( JoinPath( r2_root, "ai" ) );
    }
    candidates.push_back( "D:\\Zcoding\\OpenVSP-R2\\ai" );

    for ( const std::string& candidate : candidates )
    {
        if ( FileExists( JoinPath( candidate, "ai_design_agent.py" ) ) )
        {
            return candidate;
        }
    }
    if ( !exe_dir.empty() )
    {
        return JoinPath( exe_dir, "ai" );
    }
    return std::string();
}

std::string AIDesignWindow::ResolvePython() const
{
    const std::string env_python = GetEnvUtf8( "OPENVSP_AI_PYTHON" );
    if ( !env_python.empty() && FileExists( env_python ) )
    {
        return env_python;
    }
    const std::string ai_dir = ResolveAIDir();
    if ( !ai_dir.empty() )
    {
        const std::string bundled = JoinPath( JoinPath( ai_dir, "python" ), "python.exe" );
        if ( FileExists( bundled ) )
        {
            return bundled;
        }
    }

    // Prefer Python 3.13 (the bundled OpenVSP Python extension _vsp.pyd is cp313).
    // A generic "python.exe" on PATH may be a 3.11/venv interpreter that cannot
    // load the extension (DLL load failed), so known 3.13 locations win.
    std::vector<std::string> known;
    const std::string local = GetEnvUtf8( "LOCALAPPDATA" );
    const std::string program_files = GetEnvUtf8( "ProgramFiles" );
    const std::string program_files_x86 = GetEnvUtf8( "ProgramFiles(x86)" );
    if ( !local.empty() )
    {
        known.push_back( JoinPath( local, "Programs\\Python\\Python313\\python.exe" ) );
        known.push_back( JoinPath( local, "Programs\\Python\\Python312\\python.exe" ) );
    }
    if ( !program_files.empty() )
    {
        known.push_back( JoinPath( program_files, "Python313\\python.exe" ) );
        known.push_back( JoinPath( program_files, "Python312\\python.exe" ) );
    }
    if ( !program_files_x86.empty() )
    {
        known.push_back( JoinPath( program_files_x86, "Python313\\python.exe" ) );
    }
    known.push_back( "C:\\Python313\\python.exe" );
    for ( const std::string& candidate : known )
    {
        if ( FileExists( candidate ) )
        {
            return candidate;
        }
    }

    wchar_t found[MAX_PATH];
    if ( SearchPathW( nullptr, L"python.exe", nullptr, MAX_PATH, found, nullptr ) > 0 )
    {
        return Narrow( found );
    }
    if ( SearchPathW( nullptr, L"python3.exe", nullptr, MAX_PATH, found, nullptr ) > 0 )
    {
        return Narrow( found );
    }
    return std::string();
}

std::string AIDesignWindow::ResolveAgentScript() const
{
    const std::string ai_dir = ResolveAIDir();
    if ( ai_dir.empty() )
    {
        return std::string();
    }
    return JoinPath( ai_dir, "ai_design_agent.py" );
}

std::string AIDesignWindow::MakeOutputDir() const
{
    SYSTEMTIME st;
    GetLocalTime( &st );
    char stamp[64];
    std::snprintf( stamp, sizeof( stamp ), "%04d%02d%02d-%02d%02d%02d",
                   st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond );
    const std::string root = JoinPath( DocumentsDir(), "OpenVSP-R2\\designs" );
    const std::string dir = JoinPath( root, stamp );
    EnsureDirectory( dir );
    return dir;
}

std::string AIDesignWindow::BuildConfigJson( const std::string& output_dir, bool ga_mode ) const
{
    const int provider_index = m_ProviderChoice->value();
    const ProviderPreset& preset = kProviders[provider_index];
    std::string base_url = Trim( m_BaseUrlInput->value() ? m_BaseUrlInput->value() : "" );
    while ( !base_url.empty() && base_url.back() == '/' )
    {
        base_url.pop_back();
    }
    const std::string model = Trim( m_ModelChoice->value() ? m_ModelChoice->value() : "" );
    std::string prompt = m_PromptInput->value() ? m_PromptInput->value() : "";
    if ( Trim( prompt ).size() < 5 && ( !m_Attachments.empty() || !m_CasePath.empty() ) )
    {
        prompt = "请根据上传的技术文档与 .vsp3 案例，提取技术参数、设计输入与设备选型，"
                 "完成飞行器总体设计、修改或优化，并进行气动分析。";
    }
    const std::string ai_dir = ResolveAIDir();
    const std::string template_path = JoinPath( JoinPath( ai_dir, "templates" ),
                                                "design_report_template.docx" );

    std::ostringstream stream;
    stream << "{\n";
    stream << "  \"task\": \"" << ( ga_mode ? "ga" : "design" ) << "\",\n";
    if ( ga_mode )
    {
        stream << "  \"ga\": { \"population\": 8, \"generations\": 4, "
                  "\"time_budget_s\": 300, \"objective\": \"max_ld\" },\n";
        const std::string seed = ( !m_CasePath.empty() && FileExists( m_CasePath ) )
                                     ? m_CasePath : m_LastModelPath;
        if ( !seed.empty() && FileExists( seed ) )
        {
            stream << "  \"seed_model\": \"" << JsonEscape( seed ) << "\",\n";
        }
    }
    stream << "  \"provider\": \"" << JsonEscape( preset.id ) << "\",\n";
    stream << "  \"base_url\": \"" << JsonEscape( base_url ) << "\",\n";
    stream << "  \"api_key\": \"\",\n";
    stream << "  \"api_key_env\": \"OPENVSP_AI_API_KEY\",\n";
    stream << "  \"model\": \"" << JsonEscape( model ) << "\",\n";
    stream << "  \"prompt\": \"" << JsonEscape( prompt ) << "\",\n";
    stream << "  \"attachments\": [";
    for ( size_t i = 0; i < m_Attachments.size(); ++i )
    {
        if ( i > 0 )
        {
            stream << ", ";
        }
        stream << "\"" << JsonEscape( m_Attachments[i] ) << "\"";
    }
    stream << "],\n";
    if ( !m_CasePath.empty() && FileExists( m_CasePath ) )
    {
        stream << "  \"case_model\": \"" << JsonEscape( m_CasePath ) << "\",\n";
    }
    stream << "  \"output_dir\": \"" << JsonEscape( output_dir ) << "\",\n";
    stream << "  \"report_template\": \"" << JsonEscape( template_path ) << "\",\n";
    stream << "  \"mode\": \"auto\",\n";
    stream << "  \"max_iterations\": 4,\n";
    stream << "  \"ui\": \"openvsp-r2\"\n";
    stream << "}\n";
    return stream.str();
}

void AIDesignWindow::FetchModels()
{
    const std::string base_url = Trim( m_BaseUrlInput->value() ? m_BaseUrlInput->value() : "" );
    if ( base_url.empty() )
    {
        fl_alert( "请先填写请求地址（example: https://api.deepseek.com/v1）。" );
        return;
    }
    const std::string key = m_ApiKeyInput->value() ? m_ApiKeyInput->value() : "";
    SetStatus( "正在获取模型列表...", false );
    Fl::check();

    std::string body;
    std::string error;
    const std::string url = ( base_url.back() == '/' ? base_url : base_url + "/" ) + "models";
    if ( !HttpGetText( url, key, body, error ) )
    {
        const int provider_index = m_ProviderChoice->value();
        const std::vector<std::string> builtin =
            SplitComma( ( provider_index >= 0 && provider_index < kProviderCount )
                            ? kProviders[provider_index].builtin_models : "" );
        if ( !builtin.empty() )
        {
            m_ModelChoice->clear();
            for ( const std::string& id : builtin )
            {
                m_ModelChoice->add( id.c_str() );
            }
            m_ModelChoice->value( 0 );
            SetStatus( "获取模型列表失败，已使用内置清单（可手动输入模型 ID）：" + error, true );
            AppendLog( "[error] " + error );
            AppendLog( "[status] 内置模型清单: " + std::to_string( builtin.size() ) + " 个" );
            return;
        }
        SetStatus( "获取模型失败：" + error, true );
        AppendLog( "[error] " + error );
        return;
    }

    const std::vector<std::string> ids = ExtractModelIds( body );
    if ( ids.empty() )
    {
        const int provider_index = m_ProviderChoice->value();
        const std::vector<std::string> builtin =
            SplitComma( ( provider_index >= 0 && provider_index < kProviderCount )
                            ? kProviders[provider_index].builtin_models : "" );
        if ( !builtin.empty() )
        {
            m_ModelChoice->clear();
            for ( const std::string& id : builtin )
            {
                m_ModelChoice->add( id.c_str() );
            }
            m_ModelChoice->value( 0 );
            SetStatus( "未解析到模型 id，已使用内置清单（可手动输入模型 ID）。", true );
            return;
        }
        SetStatus( "服务器返回成功，但未解析到模型 id。", true );
        AppendLog( "[error] /models 响应: " + body.substr( 0, 500 ) );
        return;
    }

    m_ModelChoice->clear();
    for ( const std::string& id : ids )
    {
        m_ModelChoice->add( id.c_str() );
    }
    m_ModelChoice->value( 0 );

    const std::string provider_label = kProviders[m_ProviderChoice->value()].label;
    SetStatus( "已获取 " + std::to_string( ids.size() ) + " 个模型（" + provider_label + "）。", false );
    AppendLog( "[status] 模型列表: " + std::to_string( ids.size() ) + " 个" );
    for ( size_t i = 0; i < ids.size() && i < 12; ++i )
    {
        AppendLog( "  · " + ids[i] );
    }
}

void AIDesignWindow::StartDesign( bool ga_mode )
{
    if ( m_Running.load() )
    {
        return;
    }
    const std::string python = ResolvePython();
    const std::string agent = ResolveAgentScript();
    const std::string ai_dir = ResolveAIDir();

    if ( python.empty() )
    {
        fl_alert( "未找到 Python 运行时。\n请设置环境变量 OPENVSP_AI_PYTHON，或将 Python 放入安装目录 ai\\python\\python.exe。" );
        return;
    }
    if ( agent.empty() || !FileExists( agent ) )
    {
        fl_alert( "未找到 AI 设计后端 ai_design_agent.py。\n请确认安装目录下存在 ai 文件夹，或设置 OPENVSP_AI_DIR。" );
        return;
    }

    const char* prompt = m_PromptInput->value();
    if ( ( !prompt || std::strlen( prompt ) < 5 )
         && m_Attachments.empty() && m_CasePath.empty() )
    {
        fl_alert( "请填写设计需求描述，或上传技术文档/.vsp3 案例。" );
        return;
    }

    const std::string key = m_ApiKeyInput->value() ? m_ApiKeyInput->value() : "";
    const bool force_offline = m_OfflineCheck && m_OfflineCheck->value();
    const bool offline = ga_mode || key.empty() || force_offline;
    if ( key.empty() && !force_offline && !ga_mode )
    {
        const int choice = fl_choice( "未填写 API Key：将使用离线内置设计流程（不调用大模型），是否继续？",
                                      "取消", "离线运行", nullptr );
        if ( choice != 1 )
        {
            return;
        }
    }

    StoreCurrentConfig( false );
    SaveSettings();
    m_OutputDir = MakeOutputDir();
    const std::string config_path = JoinPath( m_OutputDir, "ai_job.json" );
    const std::string config = BuildConfigJson( m_OutputDir, ga_mode );
    std::ofstream out( Widen( config_path ), std::ios::binary );
    if ( !out )
    {
        fl_alert( "无法写入任务配置文件。" );
        return;
    }
    out.write( config.data(), static_cast<std::streamsize>( config.size() ) );
    out.close();

    std::wstring command = Widen( python );
    command = L"\"" + command + L"\" \"" + Widen( agent ) + L"\" --config \"" +
              Widen( config_path ) + L"\"";
    if ( offline )
    {
        command += L" --offline";
    }

    std::string error;
    if ( !StartProcess( Narrow( command ), m_OutputDir, error ) )
    {
        fl_alert( "启动 AI 设计进程失败：\n%s", error.c_str() );
        return;
    }

    m_ReportPath.clear();
    m_LastTask = ga_mode ? "ga" : "design";
    m_LogBuffer->text( "" );
    m_OpenButton->deactivate();
    m_StartButton->deactivate();
    m_GAButton->deactivate();
    m_ImportModelButton->deactivate();
    m_StopButton->activate();
    m_Running = true;
    m_StopRequested = false;
    SetStatus( ( ga_mode ? "遗传算法优化运行中... 输出目录: " : "AI 设计运行中... 输出目录: " )
               + m_OutputDir, false );
    AppendLog( ga_mode ? "[status] 启动遗传算法优化（GA）"
                       : "[status] 启动 AI 设计后端" );
    if ( !m_Attachments.empty() )
    {
        AppendLog( "[status] 上传文档 " + std::to_string( m_Attachments.size() )
                   + " 个，将随任务提交给 AI 后端解析：" );
        for ( const std::string& attachment : m_Attachments )
        {
            AppendLog( "         · " + BaseName( attachment ) );
        }
    }
    if ( !m_CasePath.empty() && FileExists( m_CasePath ) )
    {
        AppendLog( "[status] 上传案例: " + BaseName( m_CasePath )
                   + "（将作为修改/优化基线）" );
    }
    AppendLog( "         Python: " + python );
    AppendLog( "         Agent : " + agent );
    AppendLog( "         输出  : " + m_OutputDir );
    if ( ga_mode )
    {
        AppendLog( "[status] 任务：遗传算法优化（按当前工况优化几何，输出优化模型与 GA 报告）" );
    }
    if ( offline )
    {
        AppendLog( "[status] 离线模式：不调用大模型，使用内置规则设计。" );
    }
}

bool AIDesignWindow::StartProcess( const std::string& command, const std::string& work_dir,
                                   std::string& error )
{
    error.clear();

    SECURITY_ATTRIBUTES attributes;
    attributes.nLength = sizeof( attributes );
    attributes.lpSecurityDescriptor = nullptr;
    attributes.bInheritHandle = TRUE;

    HANDLE read_pipe = nullptr;
    HANDLE write_pipe = nullptr;
    if ( !CreatePipe( &read_pipe, &write_pipe, &attributes, 0 ) )
    {
        error = "创建管道失败。";
        return false;
    }
    SetHandleInformation( read_pipe, HANDLE_FLAG_INHERIT, 0 );

    STARTUPINFOW startup;
    std::memset( &startup, 0, sizeof( startup ) );
    startup.cb = sizeof( startup );
    startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_HIDE;
    startup.hStdOutput = write_pipe;
    startup.hStdError = write_pipe;
    startup.hStdInput = GetStdHandle( STD_INPUT_HANDLE );

    PROCESS_INFORMATION process;
    std::memset( &process, 0, sizeof( process ) );

    SetEnvironmentVariableW( L"PYTHONIOENCODING", L"utf-8" );
    SetEnvironmentVariableW( L"PYTHONUTF8", L"1" );
    const std::string api_key = m_ApiKeyInput->value() ? m_ApiKeyInput->value() : "";
    SetEnvironmentVariableW( L"OPENVSP_AI_API_KEY",
                             api_key.empty() ? nullptr : Widen( api_key ).c_str() );
    SetEnvironmentVariableW( L"OPENVSP_AI_DIR", Widen( ResolveAIDir() ).c_str() );
    SetEnvironmentVariableW( L"OPENVSP_R2_ROOT", Widen( JoinPath( ResolveAIDir(), ".." ) ).c_str() );
    std::string app_dir;
    try
    {
        app_dir = vsp::l10n::ExecutableDirectory().u8string();
    }
    catch ( ... )
    {
        app_dir.clear();
    }
    // Only override OPENVSP_ROOT when this install actually ships the Python API;
    // otherwise let the backend fall back to its own OpenVSP discovery.
    if ( !app_dir.empty()
         && FileExists( JoinPath( app_dir, "vspaero.exe" ) )
         && FileExists( JoinPath( app_dir, "python\\openvsp\\openvsp\\_vsp.pyd" ) ) )
    {
        SetEnvironmentVariableW( L"OPENVSP_ROOT", Widen( app_dir ).c_str() );
    }

    std::wstring command_line = Widen( command );
    std::vector<wchar_t> command_buffer( command_line.begin(), command_line.end() );
    command_buffer.push_back( L'\0' );

    const std::wstring work_dir_wide = Widen( work_dir );
    const BOOL ok = CreateProcessW( nullptr, command_buffer.data(), nullptr, nullptr, TRUE,
                                    CREATE_NO_WINDOW, nullptr,
                                    work_dir_wide.empty() ? nullptr : work_dir_wide.c_str(),
                                    &startup, &process );
    CloseHandle( write_pipe );
    if ( !ok )
    {
        CloseHandle( read_pipe );
        const DWORD last = GetLastError();
        error = "CreateProcess failed, error " + std::to_string( last ) + "。";
        return false;
    }

    CloseHandle( process.hThread );
    m_ProcessHandle = process.hProcess;
    m_ProcessId = process.dwProcessId;
    m_ReadPipe = read_pipe;
    m_ReaderThread = std::thread( &AIDesignWindow::ReadProcessOutput, this );
    return true;
}

void AIDesignWindow::ReadProcessOutput()
{
    HANDLE read_pipe = static_cast<HANDLE>( m_ReadPipe );
    HANDLE process = static_cast<HANDLE>( m_ProcessHandle );

    std::string pending;
    bool suppressed_thin_warning = false;
    char buffer[4096];
    DWORD received = 0;
    while ( ReadFile( read_pipe, buffer, sizeof( buffer ), &received, nullptr ) &&
            received > 0 )
    {
        pending.append( buffer, received );
        size_t pos = std::string::npos;
        while ( ( pos = pending.find( '\n' ) ) != std::string::npos )
        {
            std::string line = pending.substr( 0, pos );
            pending.erase( 0, pos + 1 );
            if ( !line.empty() && line.back() == '\r' )
            {
                line.pop_back();
            }
            if ( line.find( "VSPAERO_THIN" ) != std::string::npos
                 && line.find( "GetSetIndex" ) != std::string::npos )
            {
                // OpenVSP prints this harmless message while creating the thin
                // geometry set. GA runs it once per evaluation; keep the log clean.
                if ( !suppressed_thin_warning )
                {
                    suppressed_thin_warning = true;
                    Fl::awake( PostLogCB, new LogMessage{
                        "[warn] 已隐藏 OpenVSP 的已知无害日志（VSPAERO_THIN 初次查找），不影响计算。" } );
                }
                continue;
            }
            if ( !line.empty() )
            {
                Fl::awake( PostLogCB, new LogMessage{ line } );
            }
        }
    }
    if ( !pending.empty() )
    {
        Fl::awake( PostLogCB, new LogMessage{ pending } );
    }

    WaitForSingleObject( process, INFINITE );
    DWORD exit_code = 0;
    GetExitCodeProcess( process, &exit_code );
    CloseHandle( read_pipe );
    CloseHandle( process );
    m_ReadPipe = nullptr;
    m_ProcessHandle = nullptr;

    Fl::awake( PostFinishedCB, new FinishedMessage{ static_cast<int>( exit_code ) } );
}

void AIDesignWindow::PostLogCB( void* data )
{
    LogMessage* message = static_cast<LogMessage*>( data );
    if ( message )
    {
        if ( g_AIDesignWindow )
        {
            g_AIDesignWindow->AppendLog( message->text );
        }
        delete message;
    }
}

void AIDesignWindow::PostStatusCB( void* data )
{
    StatusMessage* message = static_cast<StatusMessage*>( data );
    if ( message )
    {
        if ( g_AIDesignWindow )
        {
            g_AIDesignWindow->SetStatus( message->text, message->error );
        }
        delete message;
    }
}

void AIDesignWindow::PostFinishedCB( void* data )
{
    FinishedMessage* message = static_cast<FinishedMessage*>( data );
    if ( message )
    {
        if ( g_AIDesignWindow )
        {
            g_AIDesignWindow->ProcessFinished( message->code );
        }
        delete message;
    }
}

void AIDesignWindow::ProcessFinished( int exit_code )
{
    m_Running = false;
    m_StartButton->activate();
    m_GAButton->activate();
    m_StopButton->deactivate();

    if ( m_ReaderThread.joinable() )
    {
        m_ReaderThread.join();
    }

    bool ok = false;
    if ( !m_OutputDir.empty() )
    {
        const std::string result_path = JoinPath( m_OutputDir, "result.json" );
        std::ifstream in( Widen( result_path ), std::ios::binary );
        if ( in )
        {
            std::ostringstream stream;
            stream << in.rdbuf();
            const std::string result = stream.str();
            const std::string ok_value = ExtractJsonBool( result, "ok" );
            ok = ( ok_value == "true" || ok_value == "True" );
            const std::string report = ExtractJsonString( result, "report_path" );
            if ( !report.empty() && FileExists( report ) )
            {
                m_ReportPath = report;
                m_OpenButton->activate();
            }
            const std::string model = ExtractJsonString( result, "model_path" );
            if ( !model.empty() && FileExists( model ) )
            {
                m_LastModelPath = model;
            }
            const std::string error = ExtractJsonString( result, "error" );
            if ( !error.empty() )
            {
                AppendLog( "[error] " + error );
            }
        }
    }

    if ( m_LastModelPath.empty() && !m_OutputDir.empty() )
    {
        m_LastModelPath = FindNewestVsp3( m_OutputDir );
    }
    if ( !m_LastModelPath.empty() && FileExists( m_LastModelPath ) )
    {
        m_ImportModelButton->activate();
    }

    if ( ok )
    {
        const bool was_ga = ( m_LastTask == "ga" );
        SetStatus( ( was_ga ? "优化完成。模型: " : "设计完成。报告: " )
                   + ( was_ga
                           ? ( m_LastModelPath.empty() ? "(未返回路径)" : m_LastModelPath )
                           : ( m_ReportPath.empty() ? "(未返回路径)" : m_ReportPath ) ), false );
        AppendLog( was_ga ? "[status] 遗传算法优化完成，可点击“导入OpenVSP”导入 OpenVSP 编辑，"
                            "或“打开设计报告”查看 GA 报告。"
                         : "[status] 设计完成，可点击“打开设计报告”或“导入OpenVSP”编辑。" );
    }
    else if ( exit_code == 0 )
    {
        SetStatus( "后端进程结束，但未读取到成功结果；请查看日志。", true );
    }
    else
    {
        SetStatus( "后端进程异常退出（exit code " + std::to_string( exit_code ) + "）。", true );
    }
}

void AIDesignWindow::StopDesign()
{
    if ( !m_Running.load() )
    {
        return;
    }
    m_StopRequested = true;
    if ( m_ProcessHandle )
    {
        TerminateProcess( static_cast<HANDLE>( m_ProcessHandle ), 1 );
    }
    SetStatus( "已请求停止 AI 设计进程。", true );
}

void AIDesignWindow::OpenReport()
{
    if ( m_ReportPath.empty() || !FileExists( m_ReportPath ) )
    {
        fl_alert( "尚未生成报告文件。" );
        return;
    }
    const std::wstring path = Widen( m_ReportPath );
    ShellExecuteW( nullptr, L"open", path.c_str(), nullptr, nullptr, SW_SHOWNORMAL );
}

void AIDesignWindow::AppendLog( const std::string& line )
{
    if ( !m_LogBuffer || !m_LogDisplay )
    {
        return;
    }
    m_LogBuffer->append( line.c_str() );
    m_LogBuffer->append( "\n" );
    const int len = m_LogBuffer->length();
    if ( len > 400000 )
    {
        m_LogBuffer->remove( 0, len - 200000 );
    }
    m_LogDisplay->insert_position( m_LogBuffer->length() );
    m_LogDisplay->show_insert_position();
}

void AIDesignWindow::SetStatus( const std::string& text, bool error )
{
    if ( !m_StatusBox )
    {
        return;
    }
    m_StatusBox->copy_label( text.c_str() );
    m_StatusBox->labelcolor( error ? FL_RED : FL_DARK_GREEN );
    m_StatusBox->redraw();
}

void AIDesignWindow::ImportCurrentModel()
{
    std::string path = m_LastModelPath;
    if ( ( path.empty() || !FileExists( path ) ) && !m_OutputDir.empty() )
    {
        path = FindNewestVsp3( m_OutputDir );
    }
    if ( path.empty() || !FileExists( path ) )
    {
        fl_alert( "还没有可导入的模型。\n请先点击“开始设计”或“遗传算法优化”。" );
        return;
    }
    if ( !g_ImportModelHandler )
    {
        fl_alert( "当前 OpenVSP 未注册模型导入接口，无法导入。" );
        return;
    }
    if ( g_ImportModelHandler( path ) )
    {
        AppendLog( "[status] 已导入模型到当前 OpenVSP：" + path );
        SetStatus( "模型已导入当前 OpenVSP，可在主窗口中编辑。", false );
        hide();
    }
    else
    {
        fl_alert( "模型导入失败：%s", path.c_str() );
    }
}

void AIDesignWindow::UploadCaseFile()
{
    if ( m_Running.load() )
    {
        fl_alert( "设计正在运行，请等待完成后再上传案例。" );
        return;
    }

    Fl_Native_File_Chooser chooser;
    chooser.title( "选择 OpenVSP 案例（.vsp3）" );
    chooser.type( Fl_Native_File_Chooser::BROWSE_FILE );
    chooser.filter( "OpenVSP 模型\t*.vsp3\n所有文件\t*.*" );
    if ( chooser.show() != 0 )
    {
        return;
    }
    const char* filename = chooser.filename();
    if ( !filename )
    {
        return;
    }
    const std::string path = filename;
    if ( !FileExists( path ) )
    {
        fl_alert( "文件不存在：%s", path.c_str() );
        return;
    }
    if ( ExtensionLower( path ) != ".vsp3" )
    {
        fl_alert( "上传案例仅支持 .vsp3 文件。" );
        return;
    }
    m_CasePath = path;
    RefreshCaseList();
    AppendLog( "[status] 已上传案例: " + path + "（将作为修改/优化基线）" );
}

void AIDesignWindow::ClearCaseFile()
{
    if ( m_CasePath.empty() )
    {
        return;
    }
    m_CasePath.clear();
    RefreshCaseList();
    AppendLog( "[status] 已清空上传案例。" );
}

void AIDesignWindow::RefreshCaseList()
{
    if ( !m_CaseOutput || !m_ClearCaseButton )
    {
        return;
    }
    if ( m_CasePath.empty() )
    {
        m_CaseOutput->value( "未上传案例（可选：.vsp3；作为修改/优化基线）" );
        m_ClearCaseButton->deactivate();
        return;
    }
    m_CaseOutput->value( BaseName( m_CasePath ).c_str() );
    m_CaseOutput->tooltip( m_CasePath.c_str() );
    m_ClearCaseButton->activate();
}

void AIDesignWindow::UploadDocuments()
{
    if ( m_Running.load() )
    {
        fl_alert( "设计正在运行，请等待完成后再上传文档。" );
        return;
    }

    Fl_Native_File_Chooser chooser;
    chooser.title( "选择设计输入文档" );
    chooser.type( Fl_Native_File_Chooser::BROWSE_MULTI_FILE );
    chooser.filter( "设计文档\t*.docx;*.pdf;*.xlsx;*.xls;*.txt;*.md;*.csv;*.json;*.log\n"
                    "所有文件\t*.*" );
    if ( chooser.show() != 0 )
    {
        return;
    }

    int added = 0;
    for ( int i = 0; i < chooser.count(); ++i )
    {
        const char* filename = chooser.filename( i );
        if ( !filename )
        {
            continue;
        }
        const std::string path = filename;
        if ( !FileExists( path ) )
        {
            continue;
        }
        if ( ExtensionLower( path ) == ".doc" )
        {
            fl_alert( "暂不支持旧版 .doc，请用 Word 另存为 .docx 后再上传。" );
            continue;
        }
        if ( !IsSupportedAttachment( path ) )
        {
            fl_alert( "不支持的文件类型：%s\n支持 docx/pdf/xlsx/xls/txt/md/csv/json/log。",
                      BaseName( path ).c_str() );
            continue;
        }
        if ( m_Attachments.size() >= 20 )
        {
            fl_alert( "最多上传 20 个文档。" );
            break;
        }
        if ( std::find( m_Attachments.begin(), m_Attachments.end(), path )
             != m_Attachments.end() )
        {
            continue;
        }
        m_Attachments.push_back( path );
        ++added;
    }

    if ( added == 0 )
    {
        return;
    }
    RefreshAttachmentList();
    AppendLog( "[status] 已添加 " + std::to_string( added )
               + " 个设计输入文档（由 AI 后端解析技术参数/设备选型并与需求合并）:" );
    for ( const std::string& attachment : m_Attachments )
    {
        AppendLog( "         · " + BaseName( attachment ) );
    }
}

void AIDesignWindow::ClearAttachments()
{
    if ( m_Attachments.empty() )
    {
        return;
    }
    m_Attachments.clear();
    RefreshAttachmentList();
    AppendLog( "[status] 已清空上传文档。" );
}

void AIDesignWindow::RefreshAttachmentList()
{
    if ( !m_AttachmentOutput || !m_ClearAttachmentsButton )
    {
        return;
    }
    if ( m_Attachments.empty() )
    {
        m_AttachmentOutput->value( "未上传文档（可选：Word/PDF/Excel/TXT 等技术资料）" );
        m_ClearAttachmentsButton->deactivate();
        return;
    }

    std::string text;
    for ( size_t i = 0; i < m_Attachments.size(); ++i )
    {
        if ( i > 0 )
        {
            text += "；";
        }
        text += BaseName( m_Attachments[i] );
    }
    text += "（" + std::to_string( m_Attachments.size() ) + " 个）";
    m_AttachmentOutput->value( text.c_str() );
    m_AttachmentOutput->tooltip( "已上传文档；AI 后端将解析其中的技术参数与设备选型" );
    m_ClearAttachmentsButton->activate();
}

void AIDesignWindow::FetchCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->FetchModels();
    }
}

void AIDesignWindow::UploadCaseCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->UploadCaseFile();
    }
}

void AIDesignWindow::ClearCaseCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->ClearCaseFile();
    }
}

void AIDesignWindow::UploadCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->UploadDocuments();
    }
}

void AIDesignWindow::ClearCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->ClearAttachments();
    }
}

void AIDesignWindow::StartCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->StartDesign( false );
    }
}

void AIDesignWindow::GACB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->StartDesign( true );
    }
}

void AIDesignWindow::ImportModelCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->ImportCurrentModel();
    }
}

void AIDesignWindow::StopCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->StopDesign();
    }
}

void AIDesignWindow::OpenCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->OpenReport();
    }
}

void AIDesignWindow::CloseCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->StoreCurrentConfig( false );
        window->SaveSettings();
        window->hide();
    }
}

std::string AIDesignWindow::CurrentProviderId() const
{
    int index = m_ProviderChoice ? m_ProviderChoice->value() : 0;
    if ( index < 0 || index >= kProviderCount )
    {
        index = 0;
    }
    return kProviders[index].id;
}

void AIDesignWindow::StoreCurrentConfig( bool explicit_save, int provider_index )
{
    if ( !m_ApiKeyInput || !m_ProviderChoice )
    {
        return;
    }
    if ( provider_index < 0 || provider_index >= kProviderCount )
    {
        provider_index = m_ProviderChoice ? m_ProviderChoice->value() : 0;
        if ( provider_index < 0 || provider_index >= kProviderCount ) provider_index = 0;
    }
    const std::string id = kProviders[provider_index].id;
    const std::string key = m_ApiKeyInput->value() ? m_ApiKeyInput->value() : "";
    const std::string base = Trim( m_BaseUrlInput && m_BaseUrlInput->value()
                                       ? m_BaseUrlInput->value() : "" );
    const std::string model = Trim( m_ModelChoice && m_ModelChoice->value()
                                        ? m_ModelChoice->value() : "" );
    if ( explicit_save )
    {
        if ( key.empty() )
        {
            m_ProviderKeys.erase( id );
        }
        else
        {
            m_ProviderKeys[id] = key;
        }
    }
    else if ( !key.empty() )
    {
        // Remember in-memory keys when switching provider so they are not lost.
        m_ProviderKeys[id] = key;
    }
    // 请求地址和模型也按服务商分别保存；否则切换服务商/重启后会回到
    // 内置默认地址，导致“Key 对但地址错”的 401/404。
    if ( !base.empty() )
    {
        m_ProviderBases[id] = base;
    }
    if ( !model.empty() )
    {
        m_ProviderModels[id] = model;
    }
}

void AIDesignWindow::SaveApiKey()
{
    StoreCurrentConfig( true );
    SaveSettings();
    const int index = m_ProviderChoice ? m_ProviderChoice->value() : 0;
    const std::string key = m_ApiKeyInput && m_ApiKeyInput->value()
                                ? m_ApiKeyInput->value() : "";
    if ( key.empty() )
    {
        SetStatus( "已清除该服务商保存的 API Key（不会写入磁盘）。", true );
        AppendLog( "[status] 已清除 " + std::string( kProviders[index].label )
                   + " 的本地 API Key。" );
        return;
    }
    SetStatus( "API Key 与请求地址已加密保存到本机，正在验证...", false );
    AppendLog( "[status] API Key、请求地址与模型已加密保存（仅本机，当前用户可解密）。" );
    StartVerifyKey();
}

std::string AIDesignWindow::GetBaseUrl() const
{
    if ( !m_BaseUrlInput || !m_BaseUrlInput->value() )
    {
        return std::string();
    }
    return Trim( m_BaseUrlInput->value() );
}

void AIDesignWindow::UpdateBaseUrlChoices( int provider_index, const std::string& preferred )
{
    if ( !m_BasePresetChoice || !m_BaseUrlInput )
    {
        return;
    }
    if ( provider_index < 0 || provider_index >= kProviderCount )
    {
        provider_index = 0;
    }
    const std::vector<std::string> urls = ProviderBaseUrls( provider_index );
    const std::vector<std::string> labels = ProviderBaseLabels( provider_index );

    m_BasePresetChoice->clear();
    for ( const std::string& label : labels )
    {
        m_BasePresetChoice->add( label.c_str() );
    }
    m_BasePresetChoice->add( "自定义…" );

    std::string want = Trim( preferred );
    if ( want.empty() && !urls.empty() )
    {
        want = urls.front();
    }
    int selected = static_cast<int>( urls.size() );  // 默认最后一项：自定义
    for ( size_t i = 0; i < urls.size(); ++i )
    {
        if ( urls[i] == want )
        {
            selected = static_cast<int>( i );
            break;
        }
    }
    m_BasePresetChoice->value( selected );
    if ( selected >= 0 && selected < static_cast<int>( urls.size() ) )
    {
        m_BaseUrlInput->value( urls[selected].c_str() );
    }
    else
    {
        m_BaseUrlInput->value( want.c_str() );
    }
}

void AIDesignWindow::BasePresetCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( !window || !window->m_BasePresetChoice || !window->m_BaseUrlInput )
    {
        return;
    }
    const int provider = window->m_ProviderChoice ? window->m_ProviderChoice->value() : 0;
    const std::vector<std::string> urls = ProviderBaseUrls( provider );
    const int selected = window->m_BasePresetChoice->value();
    if ( selected >= 0 && selected < static_cast<int>( urls.size() ) )
    {
        window->m_BaseUrlInput->value( urls[selected].c_str() );
        window->StoreCurrentConfig( false );
        window->SaveSettings();
        if ( window->m_ApiKeyInput && window->m_ApiKeyInput->value()
             && window->m_ApiKeyInput->value()[0] )
        {
            window->StartVerifyKey();
        }
    }
    else
    {
        window->m_BaseUrlInput->value( "" );
        window->m_BaseUrlInput->take_focus();
    }
}

void AIDesignWindow::BaseUrlInputCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( !window )
    {
        return;
    }
    window->StoreCurrentConfig( false );
    window->SaveSettings();
    if ( window->m_ApiKeyInput && window->m_ApiKeyInput->value()
         && window->m_ApiKeyInput->value()[0] )
    {
        window->StartVerifyKey();
    }
}

void AIDesignWindow::SwitchProvider( int provider_index )
{
    if ( provider_index < 0 || provider_index >= kProviderCount )
    {
        return;
    }
    // 回调触发时 Fl_Choice 的 value() 已经是新服务商；当前输入框/Key 仍属于旧服务商，
    // 必须先按旧的 m_LastProviderIndex 存储，否则会把旧地址/Key 写到新服务商名下。
    StoreCurrentConfig( false, m_LastProviderIndex );
    SaveSettings();
    ApplyProvider( provider_index, true );
    const std::string id = kProviders[provider_index].id;
    const auto saved_base = m_ProviderBases.find( id );
    if ( saved_base != m_ProviderBases.end() && !saved_base->second.empty() )
    {
        UpdateBaseUrlChoices( provider_index, saved_base->second );
    }
    else
    {
        UpdateBaseUrlChoices( provider_index, "" );
    }
    const auto saved_model = m_ProviderModels.find( id );
    if ( saved_model != m_ProviderModels.end() && !saved_model->second.empty() )
    {
        m_ModelChoice->clear();
        const std::vector<std::string> builtin =
            SplitComma( kProviders[provider_index].builtin_models );
        bool found = false;
        for ( const std::string& item : builtin )
        {
            m_ModelChoice->add( item.c_str() );
            if ( item == saved_model->second ) found = true;
        }
        if ( !found )
        {
            m_ModelChoice->add( saved_model->second.c_str() );
        }
        m_ModelChoice->value( saved_model->second.c_str() );
    }
    const auto key = m_ProviderKeys.find( id );
    if ( key != m_ProviderKeys.end() && !key->second.empty() )
    {
        m_ApiKeyInput->value( key->second.c_str() );
        SetStatus( "已切换到 " + std::string( kProviders[provider_index].label )
                   + "，正在验证已保存的 API Key...", false );
        StartVerifyKey();
    }
    else
    {
        m_ApiKeyInput->value( "" );
        SetStatus( "已切换到 " + std::string( kProviders[provider_index].label )
                   + "，尚未保存 API Key；填写后点“保存”。", false );
    }
    m_LastProviderIndex = provider_index;
    StoreCurrentConfig( false, m_LastProviderIndex );
    SaveSettings();
}

void AIDesignWindow::StartVerifyKey()
{
    if ( !m_ApiKeyInput || !m_ProviderChoice )
    {
        return;
    }
    const std::string key = m_ApiKeyInput->value() ? m_ApiKeyInput->value() : "";
    const std::string base = Trim( m_BaseUrlInput->value()
                                       ? m_BaseUrlInput->value() : "" );
    if ( key.empty() )
    {
        SetStatus( "未填写 API Key，无法验证。", true );
        return;
    }
    if ( base.empty() )
    {
        SetStatus( "未填写请求地址，无法验证 API Key。", true );
        return;
    }
    if ( m_VerifyRunning.load() )
    {
        SetStatus( "正在验证 API Key，请稍候...", false );
        return;
    }
    if ( m_VerifyThread.joinable() )
    {
        m_VerifyThread.join();
    }
    const int index = m_ProviderChoice->value();
    const std::string model = m_ModelChoice && m_ModelChoice->value()
                                  ? Trim( m_ModelChoice->value() ) : "";
    m_VerifyRunning = true;
    m_VerifyThread = std::thread( [this, base, key, index, model]()
    {
        std::string url = base;
        while ( !url.empty() && url.back() == '/' )
        {
            url.pop_back();
        }
        url += "/chat/completions";
        std::string body;
        std::string error;
        bool ok = false;
        std::string text;
        if ( model.empty() )
        {
            text = "API Key 验证失败：未选择模型。";
        }
        else
        {
            // 真正发起一次最小 chat/completions 调用，验证 Key 与所选模型均可调用；
            // 仅发送极少量 token，不发送任何本地文件/业务数据。
            std::string payload = "{\"model\":\"" + JsonEscape( model )
                + "\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],"
                  "\"max_tokens\":8,\"stream\":false}";
            if ( !HttpPostJson( url, key, payload, body, error ) )
            {
                text = "Key 验证失败: " + error;
            }
            else if ( body.find( "\"error\"" ) != std::string::npos
                      || body.find( "\"choices\"" ) == std::string::npos
                      || body.find( "\"finish_reason\"" ) == std::string::npos )
            {
                text = "Key 验证失败：模型响应无效。";
            }
            else
            {
                ok = true;
                text = "Key 验证成功: " + std::string( kProviders[index].label )
                       + " / " + model;
            }
        }
        m_VerifyRunning = false;
        Fl::awake( PostVerifyCB, new VerifyMessage{ ok, text } );
    } );
}

void AIDesignWindow::PostVerifyCB( void* data )
{
    VerifyMessage* message = static_cast<VerifyMessage*>( data );
    if ( message )
    {
        if ( g_AIDesignWindow )
        {
            g_AIDesignWindow->SetStatus( message->text, !message->ok );
            g_AIDesignWindow->AppendLog( std::string( message->ok ? "[status] " : "[warn] " )
                                         + message->text );
        }
        delete message;
    }
}

void AIDesignWindow::SaveKeyCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window )
    {
        window->SaveApiKey();
    }
}

void AIDesignWindow::ModelCB( Fl_Widget*, void* data )
{
    AIDesignWindow* window = static_cast<AIDesignWindow*>( data );
    if ( window && window->m_ApiKeyInput && window->m_ApiKeyInput->value()
         && window->m_ApiKeyInput->value()[0] )
    {
        window->StoreCurrentConfig( false );
        window->SaveSettings();
        window->StartVerifyKey();
    }
}

void AIDesignWindow::LoadSettings()
{
    const std::string path = SettingsPath();
    if ( path.empty() )
    {
        return;
    }
    std::ifstream in( Widen( path ), std::ios::binary );
    if ( !in )
    {
        return;
    }
    int provider = 0;
    std::string legacy_base;
    std::string legacy_model;
    std::string legacy_key;
    std::map<std::string, std::string> keys;
    std::map<std::string, std::string> bases;
    std::map<std::string, std::string> models;
    std::string line;
    while ( std::getline( in, line ) )
    {
        const size_t equals = line.find( '=' );
        if ( equals == std::string::npos )
        {
            continue;
        }
        const std::string name = Trim( line.substr( 0, equals ) );
        const std::string value = Trim( line.substr( equals + 1 ) );
        if ( name == "provider" )
        {
            provider = std::atoi( value.c_str() );
        }
        else if ( name == "base_url" )
        {
            legacy_base = value;
        }
        else if ( name == "model" )
        {
            legacy_model = value;
        }
        else if ( name == "api_key" )
        {
            legacy_key = value;
        }
        else if ( name.rfind( "api_key_", 0 ) == 0 )
        {
            const std::string id = name.substr( 8 );
            const std::string plain = UnprotectSecret( value );
            if ( !id.empty() && !plain.empty() )
            {
                keys[id] = plain;
            }
        }
        else if ( name.rfind( "base_url_", 0 ) == 0 )
        {
            const std::string id = name.substr( 9 );
            if ( !id.empty() && !value.empty() )
            {
                bases[id] = value;
            }
        }
        else if ( name.rfind( "model_", 0 ) == 0 )
        {
            const std::string id = name.substr( 6 );
            if ( !id.empty() && !value.empty() )
            {
                models[id] = value;
            }
        }
    }
    if ( provider < 0 || provider >= kProviderCount )
    {
        provider = 0;
    }
    // migrate old single-key/single-address format to the current provider
    if ( !legacy_key.empty() )
    {
        const std::string plain = UnprotectSecret( legacy_key );
        const std::string current_id = kProviders[provider].id;
        if ( !plain.empty() && keys.find( current_id ) == keys.end() )
        {
            keys[current_id] = plain;
        }
    }
    if ( !legacy_base.empty() )
    {
        const std::string current_id = kProviders[provider].id;
        if ( bases.find( current_id ) == bases.end() )
        {
            bases[current_id] = legacy_base;
        }
    }
    if ( !legacy_model.empty() )
    {
        const std::string current_id = kProviders[provider].id;
        if ( models.find( current_id ) == models.end() )
        {
            models[current_id] = legacy_model;
        }
    }
    m_ProviderKeys = keys;
    m_ProviderBases = bases;
    m_ProviderModels = models;
    m_ProviderChoice->value( provider );
    m_LastProviderIndex = provider;
    ApplyProvider( provider, false );

    const std::string id = kProviders[provider].id;
    const std::vector<std::string> default_bases = ProviderBaseUrls( provider );
    std::string base = default_bases.empty() ? std::string() : default_bases.front();
    const auto saved_base = m_ProviderBases.find( id );
    if ( saved_base != m_ProviderBases.end() && !saved_base->second.empty() )
    {
        base = saved_base->second;
    }
    UpdateBaseUrlChoices( provider, base );

    m_ModelChoice->clear();
    const std::vector<std::string> builtin = SplitComma( kProviders[provider].builtin_models );
    for ( const std::string& item : builtin )
    {
        m_ModelChoice->add( item.c_str() );
    }
    std::string model = builtin.empty() ? std::string() : builtin.front();
    const auto saved_model = m_ProviderModels.find( id );
    if ( saved_model != m_ProviderModels.end() && !saved_model->second.empty() )
    {
        model = saved_model->second;
    }
    bool model_found = false;
    for ( const std::string& item : builtin )
    {
        if ( item == model )
        {
            model_found = true;
            break;
        }
    }
    if ( !model.empty() )
    {
        if ( !model_found )
        {
            m_ModelChoice->add( model.c_str() );
        }
        m_ModelChoice->value( model.c_str() );
    }

    const auto key = m_ProviderKeys.find( id );
    if ( key != m_ProviderKeys.end() && !key->second.empty() )
    {
        m_ApiKeyInput->value( key->second.c_str() );
    }
}

void AIDesignWindow::SaveSettings() const
{
    const std::string path = SettingsPath();
    if ( path.empty() )
    {
        return;
    }
    std::ofstream out( Widen( path ), std::ios::binary | std::ios::trunc );
    if ( !out )
    {
        return;
    }
    const std::string base = m_BaseUrlInput && m_BaseUrlInput->value()
                                 ? Trim( m_BaseUrlInput->value() ) : "";
    const std::string model = m_ModelChoice && m_ModelChoice->value()
                                  ? Trim( m_ModelChoice->value() ) : "";
    out << "provider=" << m_ProviderChoice->value() << "\n";
    out << "base_url=" << base << "\n";
    out << "model=" << model << "\n";
    for ( const auto& item : m_ProviderBases )
    {
        if ( !item.first.empty() && !item.second.empty() )
        {
            out << "base_url_" << item.first << "=" << item.second << "\n";
        }
    }
    for ( const auto& item : m_ProviderModels )
    {
        if ( !item.first.empty() && !item.second.empty() )
        {
            out << "model_" << item.first << "=" << item.second << "\n";
        }
    }
    // Only DPAPI-encrypted blobs are written; plaintext key never touches disk.
    for ( const auto& item : m_ProviderKeys )
    {
        if ( item.first.empty() || item.second.empty() )
        {
            continue;
        }
        const std::string protected_key = ProtectSecret( item.second );
        if ( !protected_key.empty() )
        {
            out << "api_key_" << item.first << "=" << protected_key << "\n";
        }
    }
}


