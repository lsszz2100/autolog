; 오토로그(AutoLog) 설치 프로그램 (Inno Setup 6)
; packaging\build_installer.bat 이 PyInstaller 빌드 후 호출. 결과: packaging\output\NaverBlogStudio-Setup-<버전>.exe
#define AppName "오토로그"
; 버전은 build_installer.bat 이 version.py 에서 읽어 /DAppVersion= 으로 전달
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppExe "NaverBlogStudio.exe"
; 크몽 판매판: build_kmong_setup.bat 이 /DEdition=kmong 으로 호출 (검색 데이터 수집 기능 끔, config.py EDITION)
#ifdef Edition
  #define SetupSuffix "-" + Edition
#else
  #define SetupSuffix ""
#endif

[Setup]
AppId={{3FEF7D74-D228-4200-B0C2-E2D25B6C5F96}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
; 관리자 권한 없이 사용자 계정에 설치 (UAC 창 없음). 설정·데이터는 %LOCALAPPDATA%\NaverBlogStudio
PrivilegesRequired=lowest
; 새 설치 기본 폴더 (기존 설치는 업데이트 때 원래 폴더를 그대로 사용 — UsePreviousAppDir 기본값)
DefaultDirName={localappdata}\Programs\AutoLog
DisableDirPage=yes
DisableProgramGroupPage=yes
; Chromium·Playwright 최신 버전이 Windows 10 이상 64비트만 지원
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=NaverBlogStudio-Setup-{#AppVersion}{#SetupSuffix}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
; 멀티스레드 압축 (16코어 기준 7분 → 1~2분, 압축률 거의 동일)
LZMANumBlockThreads=8
LZMAUseSeparateProcess=yes
WizardStyle=modern
; 실행 중이면 설치/제거 전에 종료 요청
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 작업:"

[Files]
Source: "..\dist\NaverBlogStudio\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; 프로그램 창용 Microsoft Edge WebView2 런타임 설치기 (없는 PC에서만 실행, Windows 11·대부분의 10에는 기본 포함)
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2
#ifdef Edition
Source: "edition_{#Edition}.txt"; DestDir: "{app}"; DestName: "edition.txt"; Flags: ignoreversion
#endif

[InstallDelete]
; 예전 이름 바로가기 정리
Type: files; Name: "{autoprograms}\Naver Blog Studio.lnk"
Type: files; Name: "{autodesktop}\Naver Blog Studio.lnk"
; 이전 버전의 Chromium/라이브러리 찌꺼기 정리 (설정·데이터는 별도 폴더라 영향 없음)
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\ms-playwright"
; 예전에 받아 둔 코드 업데이트 정리 → 이 설치 파일에 든 코드로 시작 (설정·로그인·이력은 그대로)
Type: filesandordirs; Name: "{localappdata}\NaverBlogStudio\appcode"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Microsoft Edge WebView2 런타임 설치 중..."; Flags: waituntilterminated; Check: NeedsWebView2
Filename: "{app}\{#AppExe}"; Description: "{#AppName} 실행"; Flags: nowait postinstall skipifsilent
; 프로그램 안 [업데이트]로 무인 설치(/UPDATE)된 경우 설치 후 새 버전을 자동 실행
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsUpdate

[Messages]
korean.WinVersionTooLowError=이 프로그램은 Windows 10 이상(64비트)에서만 실행됩니다.%n%nWindows 7/8.1은 브라우저(Chromium) 지원이 종료되어 설치할 수 없습니다.

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2Version(Root: Integer; SubKey: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, SubKey, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function IsUpdate: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

// 제거할 때 개인 데이터(설정·로그인 세션·발행 이력·업로드 사진)와 저장된 비밀번호·API 키도 지울지 묻기
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  R: Integer;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    if MsgBox('설정, 블로그 로그인 정보, 저장된 비밀번호·API 키, 발행 기록도 모두 삭제할까요?' + #13#10 + #13#10 +
              '다시 설치해서 계속 사용하려면 [아니요]를 누르세요.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\NaverBlogStudio'), True, True, True);
      Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoProfile -ExecutionPolicy Bypass -Command "cmdkey /list | Select-String ''target=(\S*NaverBlogStudio)\s*$'' | ForEach-Object { cmdkey /delete:($_.Matches[0].Groups[1].Value) | Out-Null }"',
        '', SW_HIDE, ewWaitUntilTerminated, R);
    end;
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasWebView2Version(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key) or
                 HasWebView2Version(HKLM, 'SOFTWARE\' + WebView2Key) or
                 HasWebView2Version(HKCU, 'Software\' + WebView2Key));
end;
