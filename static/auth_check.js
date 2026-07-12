/**
 * 公共鉴权检测模块
 * 所有模块页面引入此文件，实现：
 * 1. 定期检测 token 状态
 * 2. 失效时静默尝试刷新
 * 3. 刷新失败才弹出 Cookie 输入弹窗
 */
(function () {
    'use strict';

    const SILENT_REFRESH_COOLDOWN = 60000; // 60 秒冷却
    const CHECK_INTERVAL = 15000; // 15 秒轮询
    let lastSilentRefreshTime = 0;
    let cookieModalOpen = false;
    let modalInjected = false;

    // 注入 Cookie 弹窗 DOM（仅在需要时注入一次）
    function injectCookieModal() {
        if (modalInjected) return;
        modalInjected = true;

        const overlay = document.createElement('div');
        overlay.id = 'authCookieModal';
        overlay.style.cssText = `
            display:none; position:fixed; top:0; left:0; width:100%; height:100%;
            background:rgba(0,0,0,0.7); z-index:99999;
            justify-content:center; align-items:center;
        `;
        overlay.innerHTML = `
            <div style="background:#161a22; border:1px solid rgba(255,152,0,0.4); border-radius:16px;
                        padding:30px; min-width:420px; max-width:500px; box-shadow:0 20px 60px rgba(0,0,0,0.8);">
                <h4 style="color:#ff9800; margin:0 0 12px 0; font-size:1.2rem;">⚠️ 鉴权已失效</h4>
                <p style="color:#94a3b8; font-size:0.9rem; margin-bottom:16px;">
                    Token 自动刷新失败。请前往 klwms 系统，按 F12 复制网络请求中的完整 Cookie 字符串并粘贴在下方：
                </p>
                <textarea id="authCookieInput" rows="4" style="
                    width:100%; background:#0b0e14; color:#fff; border:1px solid #334155;
                    border-radius:8px; padding:10px; margin-bottom:16px; font-size:0.85rem; resize:vertical;
                " placeholder="在此粘贴完整的 Cookie 字符串..."></textarea>
                <div style="display:flex; gap:12px; justify-content:flex-end;">
                    <button id="authCookieCancelBtn" style="
                        padding:8px 18px; border-radius:8px; border:1px solid #334155;
                        background:transparent; color:#94a3b8; cursor:pointer; font-size:0.85rem;
                    ">稍后再说</button>
                    <button id="authCookieSubmitBtn" style="
                        padding:8px 18px; border-radius:8px; border:none;
                        background:#00c6ff; color:#000; cursor:pointer; font-weight:bold; font-size:0.85rem;
                    ">确认更新</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        document.getElementById('authCookieCancelBtn').addEventListener('click', closeCookieModal);
        document.getElementById('authCookieSubmitBtn').addEventListener('click', submitCookie);
    }

    function openCookieModal() {
        if (cookieModalOpen) return;
        injectCookieModal();
        const modal = document.getElementById('authCookieModal');
        modal.style.display = 'flex';
        cookieModalOpen = true;
    }

    function closeCookieModal() {
        const modal = document.getElementById('authCookieModal');
        if (modal) modal.style.display = 'none';
        cookieModalOpen = false;
    }

    async function submitCookie() {
        const input = document.getElementById('authCookieInput');
        const val = input.value.trim();
        if (!val) { alert('Cookie 不能为空！'); return; }

        const btn = document.getElementById('authCookieSubmitBtn');
        btn.textContent = '提交中...';
        btn.disabled = true;

        try {
            const res = await fetch('/api/update_cookie', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cookie: val })
            });
            const data = await res.json();
            if (data.ok) {
                closeCookieModal();
                input.value = '';
                console.log('[AUTH] Cookie 更新成功');
            } else {
                alert('更新失败：' + data.msg);
            }
        } catch (e) {
            alert('网络异常，请重试');
        } finally {
            btn.textContent = '确认更新';
            btn.disabled = false;
        }
    }

    // 核心检测逻辑
    async function checkAuthStatus() {
        try {
            const res = await fetch('/api/cookie_status');
            const data = await res.json();

            if (data.ok) {
                // 鉴权正常，无需操作
                return;
            }

            // 鉴权失效，检查冷却期
            const now = Date.now();
            if (now - lastSilentRefreshTime > SILENT_REFRESH_COOLDOWN) {
                // 静默尝试刷新
                lastSilentRefreshTime = now;
                try {
                    const refreshRes = await fetch('/api/refresh_token', { method: 'POST' });
                    const refreshData = await refreshRes.json();
                    if (refreshData.ok) {
                        console.log('[AUTH] Token 静默刷新成功');
                        return; // 刷新成功，不弹窗
                    }
                } catch (refreshErr) {
                    console.warn('[AUTH] Token 静默刷新异常:', refreshErr);
                }
            }

            // 静默刷新失败，弹出 Cookie 输入弹窗
            if (!cookieModalOpen) {
                openCookieModal();
            }
        } catch (e) {
            // 网络异常，静默忽略
        }
    }

    // 页面加载后启动检测
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    function init() {
        // 首次检测延迟 2 秒（等页面渲染完）
        setTimeout(checkAuthStatus, 2000);
        // 定期轮询
        setInterval(checkAuthStatus, CHECK_INTERVAL);
    }

    // 暴露到全局，方便手动触发
    window.authCheck = { check: checkAuthStatus, openCookieModal: openCookieModal };
})();
