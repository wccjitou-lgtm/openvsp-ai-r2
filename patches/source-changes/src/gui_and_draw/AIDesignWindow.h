//
// OpenVSP R2 AI Design Window
// This file is released under the terms of the NASA Open Source Agreement (NOSA)
// version 1.3 as detailed in the LICENSE file which accompanies this software.
//
// Built-in AI design assistant for the Simplified-Chinese OpenVSP R2 build.
// The window collects provider/API/model/prompt settings, queries the
// OpenAI-compatible /models endpoint, then runs the bundled Python agent
// (ai/ai_design_agent.py) which drives OpenVSP/VSPAERO and writes a report.
//

#ifndef AI_DESIGN_WINDOW_H
#define AI_DESIGN_WINDOW_H

#include <atomic>
#include <functional>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include <FL/Fl_Window.H>
#include <FL/Fl_Text_Buffer.H>

class Fl_Box;
class Fl_Button;
class Fl_Check_Button;
class Fl_Choice;
class Fl_Input;
class Fl_Input_Choice;
class Fl_Multiline_Input;
class Fl_Output;
class Fl_Text_Display;

class AIDesignWindow : public Fl_Window
{
public:
    static AIDesignWindow* GetInstance();
    static void ShowWindow();
    // The main GUI registers a callback that loads a generated .vsp3 into the
    // current OpenVSP session (for editing after AI/GA optimization).
    static void SetImportModelHandler( std::function<bool( const std::string& )> handler );

    void AppendLog( const std::string& line );
    void SetStatus( const std::string& text, bool error = false );
    void ProcessFinished( int exit_code );

protected:
    AIDesignWindow();
    ~AIDesignWindow() override;

    void BuildWidgets();
    void ApplyProvider( int provider_index, bool reset_base );
    void LoadSettings();
    void SaveSettings() const;

    std::string BuildConfigJson( const std::string& output_dir, bool ga_mode ) const;
    std::string ResolveAIDir() const;
    std::string ResolvePython() const;
    std::string ResolveAgentScript() const;
    std::string MakeOutputDir() const;
    void FetchModels();
    void UploadDocuments();
    void ClearAttachments();
    void UploadCaseFile();
    void ClearCaseFile();
    void RefreshCaseList();
    void SwitchProvider( int provider_index );
    void UpdateBaseUrlChoices( int provider_index, const std::string& preferred );
    std::string GetBaseUrl() const;
    void SaveApiKey();
    void StoreCurrentConfig( bool explicit_save, int provider_index = -1 );
    void StartVerifyKey();
    std::string CurrentProviderId() const;
    void RefreshAttachmentList();
    void StartDesign( bool ga_mode );
    void ImportCurrentModel();
    void StopDesign();
    void OpenReport();
    void ShowMCPTutorial();
    bool StartProcess( const std::string& command, const std::string& work_dir,
                       std::string& error );
    void ReadProcessOutput();

    static void ProviderCB( Fl_Widget* w, void* data );
    static void FetchCB( Fl_Widget* w, void* data );
    static void UploadCB( Fl_Widget* w, void* data );
    static void ClearCB( Fl_Widget* w, void* data );
    static void UploadCaseCB( Fl_Widget* w, void* data );
    static void ClearCaseCB( Fl_Widget* w, void* data );
    static void SaveKeyCB( Fl_Widget* w, void* data );
    static void ModelCB( Fl_Widget* w, void* data );
    static void BasePresetCB( Fl_Widget* w, void* data );
    static void BaseUrlInputCB( Fl_Widget* w, void* data );
    static void PostVerifyCB( void* data );
    static void StartCB( Fl_Widget* w, void* data );
    static void GACB( Fl_Widget* w, void* data );
    static void ImportModelCB( Fl_Widget* w, void* data );
    static void StopCB( Fl_Widget* w, void* data );
    static void OpenCB( Fl_Widget* w, void* data );
    static void MCPTutorialCB( Fl_Widget* w, void* data );
    static void CloseCB( Fl_Widget* w, void* data );
    static void PostLogCB( void* data );
    static void PostStatusCB( void* data );
    static void PostFinishedCB( void* data );

    Fl_Choice* m_ProviderChoice = nullptr;
    Fl_Choice* m_BasePresetChoice = nullptr;
    Fl_Input* m_BaseUrlInput = nullptr;
    Fl_Input* m_ApiKeyInput = nullptr;
    Fl_Button* m_FetchButton = nullptr;
    Fl_Button* m_SaveKeyButton = nullptr;
    Fl_Input_Choice* m_ModelChoice = nullptr;
    Fl_Multiline_Input* m_PromptInput = nullptr;
    Fl_Button* m_UploadButton = nullptr;
    Fl_Output* m_AttachmentOutput = nullptr;
    Fl_Button* m_ClearAttachmentsButton = nullptr;
    Fl_Button* m_UploadCaseButton = nullptr;
    Fl_Output* m_CaseOutput = nullptr;
    Fl_Button* m_ClearCaseButton = nullptr;
    Fl_Check_Button* m_OfflineCheck = nullptr;
    Fl_Text_Display* m_LogDisplay = nullptr;
    Fl_Text_Buffer* m_LogBuffer = nullptr;
    Fl_Box* m_StatusBox = nullptr;
    Fl_Button* m_StartButton = nullptr;
    Fl_Button* m_GAButton = nullptr;
    Fl_Button* m_ImportModelButton = nullptr;
    Fl_Button* m_StopButton = nullptr;
    Fl_Button* m_OpenButton = nullptr;
    Fl_Button* m_MCPButton = nullptr;
    Fl_Button* m_CloseButton = nullptr;

    std::string m_ReportPath;
    std::string m_OutputDir;
    std::string m_LastModelPath;
    std::string m_LastTask;
    std::vector<std::string> m_Attachments;
    std::string m_CasePath;
    // provider id -> plaintext key, kept only in memory; persisted only as DPAPI blobs.
    std::map<std::string, std::string> m_ProviderKeys;
    std::map<std::string, std::string> m_ProviderBases;
    std::map<std::string, std::string> m_ProviderModels;
    int m_LastProviderIndex = 0;
    std::atomic<bool> m_VerifyRunning{ false };
    std::thread m_VerifyThread;
    std::atomic<bool> m_Running{ false };
    std::atomic<bool> m_StopRequested{ false };
    std::thread m_ReaderThread;
    void* m_ProcessHandle = nullptr;   // HANDLE, kept void* to avoid <windows.h>
    void* m_ReadPipe = nullptr;        // HANDLE
    unsigned long m_ProcessId = 0;
    bool m_SaveKey = true;
};

#endif // AI_DESIGN_WINDOW_H
