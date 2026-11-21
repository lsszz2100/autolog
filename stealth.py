"""Playwright Chromium N사 안티봇 탐지 우회 (Stealth) 모듈.

N사의 봇 탐지 시그니처를 완전히 우회합니다:
  1. navigator.webdriver === false / undefined
  2. navigator.plugins / mimeTypes 플러그인 에뮬레이션
  3. navigator.languages ['ko-KR', 'ko', 'en-US', 'en']
  4. window.chrome 런타임 객체 모방
  5. navigator.permissions.query('notifications') 응답 패치
  6. WebGL SwiftShader 마스킹 -> Intel Iris Xe / Qualcomm Adreno
  7. navigator.platform ('Win32') 및 CPU 코어, 메모리 모방
  8. Notification.permission 정상화
"""

_STEALTH_JS = """
(() => {
  const isMobile = false;

  // 1) navigator.webdriver 무력화
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  // 2) window.chrome 런타임 객체 모방
  try {
    if (!window.chrome) {
      window.chrome = {
        runtime: {},
        loadTimes: function () {
          return {
            commitLoadTime: Date.now() / 1000 - 1,
            connectionInfo: 'http/1.1',
            finishDocumentLoadTime: Date.now() / 1000,
            finishLoadTime: Date.now() / 1000,
            firstPaintAfterLoadTime: 0,
            firstPaintTime: Date.now() / 1000,
            navigationType: 'Other',
            npnNegotiatedProtocol: 'unknown',
            requestTime: Date.now() / 1000 - 1,
            startLoadTime: Date.now() / 1000 - 1,
            wasAlternateProtocolAvailable: false,
            wasFetchedViaSpdy: false,
            wasNpnNegotiated: false,
          };
        },
        csi: function () {
          return {
            onloadT: Date.now(),
            pageT: Date.now() - performance.timing.navigationStart,
            startE: performance.timing.navigationStart,
            tran: 15,
          };
        },
        app: { isInstalled: false },
      };
    }
  } catch (e) {}

  // 3) navigator.permissions
  try {
    const proto = navigator.permissions && navigator.permissions.__proto__;
    if (proto && proto.query) {
      const origQuery = proto.query.bind(navigator.permissions);
      proto.query = (params) => {
        if (params && params.name === 'notifications') {
          return Promise.resolve({
            state: (typeof Notification !== 'undefined' && Notification.permission)
              ? Notification.permission : 'default',
            onchange: null,
            addEventListener: () => {},
            removeEventListener: () => {},
            dispatchEvent: () => false,
          });
        }
        return origQuery(params);
      };
    }
  } catch (e) {}

  // 4) navigator.plugins / mimeTypes
  try {
    const fakePlugins = [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
      { name: 'Chromium PDF Viewer', filename: 'chromium-pdf-viewer', description: '' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'edge-pdf-viewer', description: '' },
      { name: 'WebKit built-in PDF', filename: 'webkit-pdf-viewer', description: '' },
    ];
    Object.defineProperty(navigator, 'plugins', {
      get: () => fakePlugins, configurable: true,
    });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => [{
        type: 'application/pdf', suffixes: 'pdf',
        description: 'Portable Document Format',
        enabledPlugin: fakePlugins[0],
      }],
      configurable: true,
    });
  } catch (e) {}

  // 5) navigator.languages
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['ko-KR', 'ko', 'en-US', 'en'],
      configurable: true,
    });
  } catch (e) {}

  // 6) WebGL 벤더/렌더러 위장 (SwiftShader 봇 감지 회피)
  try {
    const patch = (proto) => {
      const orig = proto.getParameter;
      proto.getParameter = function (parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel(R) Iris(R) Xe Graphics';
        return orig.apply(this, arguments);
      };
    };
    if (typeof WebGLRenderingContext !== 'undefined')
      patch(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined')
      patch(WebGL2RenderingContext.prototype);
  } catch (e) {}

  // 7) navigator.platform / hardwareConcurrency / deviceMemory
  try {
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32', configurable: true });
    if (!('hardwareConcurrency' in navigator) || navigator.hardwareConcurrency === 0) {
      Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
    }
    if (!('deviceMemory' in navigator)) {
      Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
    }
  } catch (e) {}

  // 8) Notification.permission
  try {
    if (typeof Notification !== 'undefined') {
      Object.defineProperty(Notification, 'permission', { get: () => 'default', configurable: true });
    }
  } catch (e) {}
})();
"""

def apply_stealth(context):
    """Playwright BrowserContext에 스텔스 init script를 등록합니다."""
    context.add_init_script(_STEALTH_JS)
