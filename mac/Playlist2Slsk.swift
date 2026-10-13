// Playlist2Slsk : la vraie appli Mac.
// Une fenêtre native (WebKit) qui affiche l'interface, avec le serveur Python
// (app.py) lancé en arrière-plan et arrêté avec l'appli. Compilé sur le Mac par
// « Créer l'appli Mac.command » (swiftc des outils en ligne de commande d'Apple).

import Cocoa
import WebKit

let PORT = 8765

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var home: URL?
    var ready = false
    var log = ""
    var logFile: FileHandle?
    var quitting = false

    lazy var projectDir: URL = {
        if let p = Bundle.main.object(forInfoDictionaryKey: "P2SProjectDir") as? String,
           FileManager.default.fileExists(atPath: p + "/app.py") {
            return URL(fileURLWithPath: p)
        }
        return Bundle.main.bundleURL.deletingLastPathComponent()
    }()

    // MARK: - Démarrage

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildWindow()
        showPage(title: "Démarrage…", body: "")
        checkExisting { found in
            if found {
                self.open(URL(string: "http://127.0.0.1:\(PORT)/")!)
            } else {
                self.startServer()
            }
        }
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "pickFolder")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Playlist2Slsk"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(srgbRed: 0x15 / 255.0, green: 0x15 / 255.0, blue: 0x14 / 255.0, alpha: 1)
                : NSColor(srgbRed: 0xec / 255.0, green: 0xeb / 255.0, blue: 0xe6 / 255.0, alpha: 1)
        }
        window.minSize = NSSize(width: 820, height: 600)
        window.contentView = web
        window.delegate = self
        window.center()
        _ = window.setFrameAutosaveName("Playlist2SlskMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // Un serveur tourne déjà (lancé par Lancer.command) : on s'y branche.
    func checkExisting(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(PORT)/api/info")!)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200 && data != nil
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    func startServer() {
        let python = projectDir.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            showPage(title: "Installation incomplète",
                     body: "L'environnement Python est introuvable dans <code>\(esc(projectDir.path))</code>.<br>"
                         + "Double-clique une fois sur <b>Créer l'appli Mac.command</b> dans ce dossier, puis relance l'appli.")
            return
        }
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".playlist2slsk")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let logURL = dir.appendingPathComponent("appli.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        logFile = try? FileHandle(forWritingTo: logURL)

        var env = ProcessInfo.processInfo.environment
        let venvBin = projectDir.appendingPathComponent(".venv/bin").path
        env["PATH"] = [venvBin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
                       env["PATH"] ?? ""].joined(separator: ":")
        env["P2S_NO_BROWSER"] = "1"
        env["P2S_GUI"] = "1"
        env["P2S_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["PYTHONUNBUFFERED"] = "1"
        let sockseek = projectDir.appendingPathComponent("bin/sockseek").path
        if FileManager.default.isExecutableFile(atPath: sockseek) { env["SOCKSEEK"] = sockseek }

        let p = Process()
        p.executableURL = python
        p.arguments = [projectDir.appendingPathComponent("app.py").path]
        p.currentDirectoryURL = projectDir
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { h in
            let data = h.availableData
            if data.isEmpty { h.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self.received(text, data) }
        }
        p.terminationHandler = { proc in
            DispatchQueue.main.async { self.serverEnded(proc.terminationStatus) }
        }
        do {
            try p.run()
            server = p
        } catch {
            showPage(title: "Impossible de démarrer", body: esc(error.localizedDescription))
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) {
            if !self.ready && self.server?.isRunning == true {
                self.showPage(title: "Le démarrage traîne", body: self.logTail())
            }
        }
    }

    func received(_ text: String, _ data: Data) {
        logFile?.write(data)
        log += text
        if log.count > 20000 { log = String(log.suffix(10000)) }
        if !ready, let r = log.range(of: #"http://127\.0\.0\.1:\d+/"#, options: .regularExpression) {
            ready = true
            open(URL(string: String(log[r]))!)
        }
    }

    func serverEnded(_ status: Int32) {
        server = nil
        if quitting { return }
        showPage(title: ready ? "Le moteur s'est arrêté" : "Impossible de démarrer",
                 body: logTail() + "<p><a href=\"p2s:restart\">Relancer</a></p>")
        ready = false
    }

    func open(_ url: URL) {
        home = url
        web.load(URLRequest(url: url))
    }

    // MARK: - Pages de secours (démarrage, erreurs)

    func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
    }

    func logTail() -> String {
        let lines = log.split(separator: "\n", omittingEmptySubsequences: false).suffix(14).joined(separator: "\n")
        return lines.isEmpty ? "" : "<pre>\(esc(lines))</pre><p class=\"m\">Journal complet : ~/.playlist2slsk/appli.log</p>"
    }

    func showPage(title: String, body: String) {
        let html = """
        <!doctype html><html lang="fr"><meta charset="utf-8"><style>
        :root{--bg:#ecebe6;--fg:#111;--mut:#6b6a66;--acc:#2f4bff}
        @media (prefers-color-scheme: dark){:root{--bg:#151514;--fg:#ecebe6;--mut:#9a9893;--acc:#6b80ff}}
        html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,"Helvetica Neue",sans-serif}
        main{max-width:760px;margin:0 auto;padding:72px 40px}
        .logo{font-weight:900;font-size:22px;letter-spacing:-.03em;border-bottom:2px solid var(--fg);padding-bottom:16px}
        h1{font-weight:900;font-size:56px;line-height:.95;letter-spacing:-.04em;margin:48px 0 18px}
        pre{background:rgba(127,127,127,.12);padding:14px;font:12px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;overflow:auto;max-height:340px}
        code{font:13px ui-monospace,Menlo,monospace}.m{color:var(--mut);font-size:13px}
        a{color:var(--acc);font-weight:700;text-transform:uppercase;letter-spacing:.06em;font-size:13px}
        .bar{height:6px;border:2px solid var(--fg);margin-top:28px;overflow:hidden;width:260px}
        .bar i{display:block;height:100%;width:30%;background:var(--acc);animation:s 1.1s ease-in-out infinite alternate}
        @keyframes s{from{margin-left:0}to{margin-left:70%}}
        </style><main><div class="logo">PLAYLIST2SLSK</div><h1>\(esc(title))</h1>
        \(body.isEmpty ? "<div class=\"bar\"><i></i></div>" : body)</main></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - Liens : l'interface reste dans la fenêtre, le reste part dans le navigateur

    func isLocal(_ url: URL?) -> Bool {
        guard let host = url?.host else { return url?.scheme == "about" }
        return host == "127.0.0.1" || host == "localhost"
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.allow) }
        if url.scheme == "p2s" {
            decisionHandler(.cancel)
            if url.absoluteString == "p2s:restart" { restart() }
            return
        }
        if url.scheme != "http" && url.scheme != "https" && url.scheme != "about" && url.scheme != "data" && url.scheme != "blob" {
            // Jamais de file:// ni d'autre schéma qui lancerait un programme : seulement mail et Spotify.
            if url.scheme == "mailto" || url.scheme == "spotify" { NSWorkspace.shared.open(url) }
            return decisionHandler(.cancel)
        }
        // Un lien cliqué depuis l'interface vers un site extérieur : navigateur.
        // La connexion Spotify (redirection voulue par l'interface) reste dans la fenêtre.
        if action.navigationType == .linkActivated && !isLocal(url) && isLocal(webView.url) {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if isLocal(url) { webView.load(action.request) }
            else if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {}

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let e = error as NSError
        if e.code == NSURLErrorCancelled { return }
        if isLocal(e.userInfo[NSURLErrorFailingURLErrorKey] as? URL) && server == nil && ready == false && home != nil {
            showPage(title: "Moteur injoignable", body: "<p><a href=\"p2s:restart\">Relancer</a></p>")
        }
    }

    // MARK: - Fenêtres JavaScript (confirm, alert, fichiers)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Annuler")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    // Sélecteur de dossier natif, appelé par l'interface (pickFolder()).
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.prompt = "Choisir"
        panel.message = "Dossier de destination"
        panel.beginSheetModal(for: window) { r in
            guard r == .OK, let url = panel.url else { return replyHandler(nil, nil) }
            var path = url.path
            let h = FileManager.default.homeDirectoryForCurrentUser.path
            if path.hasPrefix(h + "/") || path == h { path = "~" + path.dropFirst(h.count) }
            replyHandler(path, nil)
        }
    }

    // MARK: - Menus

    func buildMenu() {
        let main = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem()
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            holder.submenu = m
            main.addItem(holder)
            if title == "Fenêtre" { NSApp.windowsMenu = m }
        }
        func item(_ t: String, _ sel: Selector?, _ key: String, _ mods: NSEvent.ModifierFlags = .command,
                  target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: sel, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        let hideOthers = item("Masquer les autres", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        add("Playlist2Slsk", [
            item("À propos de Playlist2Slsk", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("Masquer Playlist2Slsk", #selector(NSApplication.hide(_:)), "h"),
            hideOthers,
            item("Tout afficher", #selector(NSApplication.unhideAllApplications(_:)), ""),
            .separator(),
            item("Quitter Playlist2Slsk", #selector(NSApplication.terminate(_:)), "q"),
        ])
        add("Édition", [
            item("Annuler", Selector(("undo:")), "z"),
            item("Rétablir", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Couper", #selector(NSText.cut(_:)), "x"),
            item("Copier", #selector(NSText.copy(_:)), "c"),
            item("Coller", #selector(NSText.paste(_:)), "v"),
            item("Tout sélectionner", #selector(NSText.selectAll(_:)), "a"),
        ])
        add("Présentation", [
            item("Accueil", #selector(goHome), "1", target: self),
            item("Précédent", #selector(goBack), "[", target: self),
            item("Actualiser", #selector(reload), "r", target: self),
            .separator(),
            item("Plein écran", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        add("Fenêtre", [
            item("Réduire", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Agrandir", #selector(NSWindow.performZoom(_:)), ""),
            item("Fermer", #selector(NSWindow.performClose(_:)), "w"),
        ])
        add("Aide", [
            item("Ouvrir le dossier de l'outil", #selector(openFolder), "", target: self),
            item("Journal technique", #selector(openLog), "", target: self),
        ])
        NSApp.mainMenu = main
    }

    @objc func goHome() { if let h = home { web.load(URLRequest(url: h)) } }
    @objc func goBack() { if web.canGoBack { web.goBack() } else { goHome() } }
    @objc func reload() { if isLocal(web.url) { web.reload() } else { goHome() } }
    @objc func openFolder() { NSWorkspace.shared.open(projectDir) }
    @objc func openLog() {
        let f = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".playlist2slsk/appli.log")
        if FileManager.default.fileExists(atPath: f.path) { NSWorkspace.shared.open(f) }
    }

    func restart() {
        ready = false
        log = ""
        showPage(title: "Démarrage…", body: "")
        checkExisting { found in
            if found { self.open(URL(string: "http://127.0.0.1:\(PORT)/")!) } else { self.startServer() }
        }
    }

    // MARK: - Quitter (on prévient si un téléchargement tourne)

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApp.terminate(nil)
        return false
    }

    final class Flag: @unchecked Sendable { var value = false }

    func isBusy() -> Bool {
        guard let base = home else { return false }
        let busy = Flag()
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: base.appendingPathComponent("api/busy"))
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                busy.value = (o["busy"] as? Bool) ?? false
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return busy.value
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if server != nil && isBusy() {
            let a = NSAlert()
            a.messageText = "Un téléchargement est en cours"
            a.informativeText = "Si tu quittes, il s'arrête. Les morceaux déjà téléchargés restent en place, et tu pourras reprendre plus tard."
            a.addButton(withTitle: "Quitter")
            a.addButton(withTitle: "Continuer")
            if a.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        return .terminateNow
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        guard let p = server, p.isRunning else { return }
        p.terminate()
        let limit = Date().addingTimeInterval(3)
        while p.isRunning && Date() < limit { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }
}

@main
struct Main {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}
