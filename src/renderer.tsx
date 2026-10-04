import { jsxRenderer } from 'hono/jsx-renderer'

export const renderer = jsxRenderer(({ children, Layout, ...props }) => {
  const title = (props as any).title
    ? `${(props as any).title} — NBA 預測平台`
    : 'NBA 對戰預測平台'
  const nav = (props as any).nav as string | undefined
  const bodyScript = (props as any).script as string | undefined

  const navItems = [
    { key: 'decision', href: '/decision', icon: 'fa-compass', label: '決策中心' },
    { key: 'games', href: '/', icon: 'fa-basketball', label: '賽事總覽' },
    { key: 'injuries', href: '/injuries', icon: 'fa-kit-medical', label: '傷病中心' },
    { key: 'performance', href: '/performance', icon: 'fa-chart-line', label: '回測 / 績效' },
    { key: 'bets', href: '/bets', icon: 'fa-receipt', label: '投注紀錄' },
    { key: 'bankroll', href: '/bankroll', icon: 'fa-wallet', label: '資金' },
    { key: 'status', href: '/status', icon: 'fa-heart-pulse', label: '系統狀態' },
  ]

  return (
    <html lang="zh-Hant">
      <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>{title}</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link
          href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css"
          rel="stylesheet"
        />
        <link rel="icon" type="image/svg+xml" href="/static/favicon.svg" />
        <link href="/static/style.css" rel="stylesheet" />
      </head>
      <body class="bg-slate-950 text-slate-100 min-h-screen antialiased">
        <header id="site-header" class="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-40">
          <div class="max-w-7xl mx-auto px-4">
            <div class="flex items-center justify-between h-14">
              <a href="/" class="flex items-center gap-2 font-bold text-orange-400 shrink-0">
                <i class="fas fa-basketball"></i>
                <span class="hidden sm:inline">NBA 預測平台</span>
                <span class="sm:hidden">NBA</span>
              </a>
              <nav id="main-nav" class="flex items-center gap-1 overflow-x-auto scrollbar-none">
                {navItems.map((item) => (
                  <a
                    href={item.href}
                    class={`px-2.5 py-1.5 rounded-md text-sm whitespace-nowrap transition ${
                      nav === item.key
                        ? 'bg-orange-500/15 text-orange-300 font-medium'
                        : 'text-slate-400 hover:text-slate-100 hover:bg-slate-800'
                    }`}
                  >
                    <i class={`fas ${item.icon} mr-1 text-xs`}></i>
                    <span class="hidden md:inline">{item.label}</span>
                    <span class="md:hidden">{item.label.slice(0, 2)}</span>
                  </a>
                ))}
              </nav>
              <div id="auth-box" class="text-sm text-slate-400 shrink-0 ml-2">
                <span class="animate-pulse">…</span>
              </div>
            </div>
          </div>
        </header>

        <main id="main-content" class="max-w-7xl mx-auto px-4 py-6">
          {children}
        </main>

        <footer id="site-footer" class="border-t border-slate-800 mt-12 py-6 text-xs text-slate-500">
          <div class="max-w-7xl mx-auto px-4 space-y-2">
            <p class="text-amber-500/90">
              <i class="fas fa-triangle-exclamation mr-1"></i>
              <strong>免責聲明</strong>：本平台模型輸出僅為機率參考，
              <strong>不保證獲利</strong>。僅供個人於台灣運彩（合法管道）投注參考，
              不對外提供投注服務、不代操、不收費。
            </p>
            <p>
              台彩返還率低於國際盤，長期獲利門檻高；所有推薦與回測 ROI 均以台彩實際賠率計算。
              未滿 18 歲不得投注。
            </p>
            <p class="text-slate-600">
              階段一（網站殼子）· Hono + Cloudflare Pages ·
              資料由階段二（Python 資料工程 + ML 引擎）寫入共用資料庫
            </p>
          </div>
        </footer>

        <script src="/static/js/common.js"></script>
        {bodyScript ? <script src={bodyScript}></script> : null}
      </body>
    </html>
  )
})
