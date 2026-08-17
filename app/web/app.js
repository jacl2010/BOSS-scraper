const { createApp } = Vue;

createApp({
  data() {
    return {
      route: location.hash.slice(1) || '/resumes',
      navigation: [
        { route: '/resumes', code: 'CV', label: '简历管理' },
        { route: '/matches', code: 'AI', label: 'BOSS 岗位匹配' },
        { route: '/llm', code: 'KEY', label: 'LLM API Key' },
      ],
      llm: { base_url: '', model: '', key_configured: false, tested_at: null },
      llmForm: { base_url: '', model: '' },
      resumes: [], selectedResumeId: '', resultResumeId: '',
      conditionForm: { job_keyword: '', city: '', experience: '不限', degree: '不限', salary: '不限', monitor_enabled: false },
      boss: { state: 'unknown', message: '' },
      matchStatus: { status: 'idle', stage: 'idle', progress_current: 0, progress_total: 0, message: '' },
      results: { new_published: [], new_active: [] }, resultTab: 'new_published',
      busy: { llm: false, upload: false, resume: false, boss: false },
      notice: { type: 'info', text: '' }, pollTimer: null,
      stages: [
        { key: 'checking', code: '01', label: '检查' }, { key: 'scraping', code: '02', label: '采集' },
        { key: 'filtering', code: '03', label: '过滤' }, { key: 'scoring', code: '04', label: '评分' },
        { key: 'finalizing', code: '05', label: '整理' },
      ],
    };
  },
  computed: {
    pageMeta() {
      return {
        '/resumes': { eyebrow: 'RESUME WORKBENCH', title: '把简历变成可匹配的信号', description: '上传文本型 PDF，检查 AI 摘要，并为每份简历保存独立条件。' },
        '/matches': { eyebrow: 'MANUAL MATCH RUN', title: '只在你点击时开始匹配', description: '先做五项硬过滤，再由 LLM 评分，保留两个互斥 Top 10。' },
        '/llm': { eyebrow: 'MODEL CONNECTION', title: '连接你信任的大模型', description: '页面只保存服务地址和模型；密钥始终停留在环境变量中。' },
      }[this.route] || { eyebrow: '', title: '', description: '' };
    },
    selectedResume() { return this.resumes.find((item) => item.id === this.selectedResumeId); },
    eligibleResumes() { return this.resumes.filter((item) => item.status === 'ready' && item.monitor_enabled && this.conditionsComplete(item.conditions)); },
    currentResults() { return this.results[this.resultTab] || []; },
    canStartMatch() { return this.matchStatus.status !== 'running' && this.llm.tested_at && this.llm.key_configured && this.eligibleResumes.length > 0; },
    taskLabel() { return ({ idle: '空闲', running: '运行中', completed: '已完成', failed: '失败' })[this.matchStatus.status] || '空闲'; },
    bossLabel() { return ({ unknown: '尚未检查', ready: '已就绪', login_required: '需要登录', platform_limited: '访问受限', chrome_unavailable: 'Chrome 不可用', collector_unavailable: '采集器不可用' })[this.boss.state] || this.boss.state; },
  },
  async mounted() {
    addEventListener('hashchange', this.onRouteChange);
    await Promise.all([this.loadLlm(), this.loadResumes(), this.loadMatchStatus()]);
    if (this.matchStatus.status === 'running') this.beginPolling();
  },
  beforeUnmount() { removeEventListener('hashchange', this.onRouteChange); this.stopPolling(); },
  methods: {
    async api(path, options = {}) {
      const response = await fetch(path, options);
      if (response.status === 204) return null;
      let payload = null;
      try { payload = await response.json(); } catch (_) { payload = null; }
      if (!response.ok) throw new Error(payload?.error?.message || payload?.detail || `请求失败（${response.status}）`);
      return payload;
    },
    onRouteChange() {
      this.route = location.hash.slice(1) || '/resumes';
      if (this.route !== '/matches') this.stopPolling();
      else if (this.matchStatus.status === 'running') this.beginPolling();
    },
    flash(text, type = 'info') { this.notice = { text, type }; },
    async loadLlm() { const data = await this.api('/api/llm-settings'); if (data) { this.llm = data; this.llmForm = { base_url: data.base_url, model: data.model }; } },
    async saveLlm() {
      this.busy.llm = true;
      try { this.llm = await this.api('/api/llm-settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.llmForm) }); this.flash('大模型连接已验证并保存。', 'success'); }
      catch (error) { this.flash(error.message, 'error'); } finally { this.busy.llm = false; }
    },
    async loadResumes() { this.resumes = await this.api('/api/resumes'); if (!this.selectedResumeId && this.resumes.length) this.selectResume(this.resumes[0].id); },
    selectResume(id) {
      this.selectedResumeId = id; const resume = this.resumes.find((item) => item.id === id); const c = resume?.conditions || {};
      this.conditionForm = { job_keyword: c.job_keyword || '', city: c.city || '', experience: c.experience || '不限', degree: c.degree || '不限', salary: c.salary || '不限', monitor_enabled: Boolean(resume?.monitor_enabled) };
    },
    async uploadFiles(event) {
      const files = [...event.target.files]; event.target.value = ''; this.busy.upload = true;
      for (const file of files) {
        if (this.resumes.length >= 10) { this.flash('已达到 10 份简历上限。', 'error'); break; }
        const body = new FormData(); body.append('file', file);
        try { const resume = await this.api('/api/resumes', { method: 'POST', body }); this.resumes.push(resume); this.selectResume(resume.id); }
        catch (error) { this.flash(`${file.name}：${error.message}`, 'error'); }
      }
      this.busy.upload = false;
    },
    async saveConditions() {
      if (!this.selectedResume) return; this.busy.resume = true;
      const { monitor_enabled, ...conditions } = this.conditionForm;
      try { const updated = await this.api(`/api/resumes/${this.selectedResume.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conditions, monitor_enabled }) }); this.replaceResume(updated); this.flash('简历条件已保存。', 'success'); }
      catch (error) { this.conditionForm.monitor_enabled = false; this.flash(error.message, 'error'); } finally { this.busy.resume = false; }
    },
    async retryParse() { try { this.replaceResume(await this.api(`/api/resumes/${this.selectedResume.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ retry_parse: true }) })); } catch (error) { this.flash(error.message, 'error'); } },
    async deleteResume() {
      if (!this.selectedResume || !confirm(`删除 ${this.selectedResume.filename}？此操作无法恢复。`)) return;
      try { await this.api(`/api/resumes/${this.selectedResume.id}`, { method: 'DELETE' }); this.resumes = this.resumes.filter((item) => item.id !== this.selectedResumeId); this.selectedResumeId = ''; if (this.resumes.length) this.selectResume(this.resumes[0].id); this.flash('简历已删除。', 'success'); } catch (error) { this.flash(error.message, 'error'); }
    },
    replaceResume(updated) { const index = this.resumes.findIndex((item) => item.id === updated.id); if (index >= 0) this.resumes.splice(index, 1, updated); this.selectResume(updated.id); },
    async checkBoss() { this.busy.boss = true; try { this.boss = await this.api('/api/boss/status'); } catch (error) { this.flash(error.message, 'error'); } finally { this.busy.boss = false; } },
    async setupBoss() { this.busy.boss = true; try { this.boss = await this.api('/api/boss/setup', { method: 'POST' }); this.flash('已打开 BOSS 专用 Chrome，请手动登录后重新检查。'); } catch (error) { this.flash(error.message, 'error'); } finally { this.busy.boss = false; } },
    async startMatch() { try { this.matchStatus = await this.api('/api/matches', { method: 'POST' }); this.beginPolling(); } catch (error) { this.flash(error.message, 'error'); } },
    async loadMatchStatus() { this.matchStatus = await this.api('/api/matches/status'); },
    beginPolling() { this.stopPolling(); this.pollTimer = setInterval(async () => { try { await this.loadMatchStatus(); if (['completed', 'failed'].includes(this.matchStatus.status)) { this.stopPolling(); await this.loadResumes(); if (this.resultResumeId) await this.loadResults(); } } catch (error) { this.stopPolling(); this.flash('本地服务已断开，请重新双击启动。', 'error'); } }, 1000); },
    stopPolling() { if (this.pollTimer) clearInterval(this.pollTimer); this.pollTimer = null; },
    async loadResults() { if (!this.resultResumeId) { this.results = { new_published: [], new_active: [] }; return; } try { this.results = await this.api(`/api/resumes/${this.resultResumeId}/results`); } catch (error) { this.flash(error.message, 'error'); } },
    conditionsComplete(c) { return Boolean(c?.job_keyword?.trim() && c?.city?.trim() && c?.experience?.trim() && c?.degree?.trim() && c?.salary?.trim()); },
    stageClass(key) { const order = this.stages.map((item) => item.key); const current = order.indexOf(this.matchStatus.stage); const index = order.indexOf(key); return { active: index === current && this.matchStatus.status === 'running', done: (current > index) || this.matchStatus.status === 'completed' }; },
    resumeStatusLabel(status) { return ({ parsing: '解析中', ready: '已完成', parse_failed: '失败' })[status] || status; },
    formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; },
    join(values) { return values?.length ? values.join('、') : '—'; },
  },
}).mount('#app');
