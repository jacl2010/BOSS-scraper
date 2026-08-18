const { createApp } = Vue;

const SelectMenu = {
  inheritAttrs: false,
  props: {
    modelValue: { type: [String, Number], default: '' },
    options: { type: Array, default: () => [] },
    placeholder: { type: String, default: '请选择' },
    disabled: Boolean,
    labelKey: { type: String, default: '' },
    valueKey: { type: String, default: '' },
  },
  emits: ['update:modelValue', 'change'],
  data() { return { open: false, openUpward: false, popoverMaxHeight: 248 }; },
  computed: {
    selected() { return this.options.find((option) => this.optionValue(option) === this.modelValue); },
    selectedLabel() { return this.selected ? this.optionLabel(this.selected) : ''; },
  },
  mounted() {
    document.addEventListener('pointerdown', this.closeOnOutside);
    window.addEventListener('resize', this.syncPopoverPosition);
    window.addEventListener('scroll', this.syncPopoverPosition, true);
  },
  beforeUnmount() {
    document.removeEventListener('pointerdown', this.closeOnOutside);
    window.removeEventListener('resize', this.syncPopoverPosition);
    window.removeEventListener('scroll', this.syncPopoverPosition, true);
  },
  methods: {
    optionLabel(option) { return option?.label ?? (this.labelKey ? option?.[this.labelKey] : option); },
    optionValue(option) { return option?.value ?? (this.valueKey ? option?.[this.valueKey] : option); },
    toggle() {
      if (this.disabled) return;
      this.open = !this.open;
      if (this.open) this.$nextTick(this.syncPopoverPosition);
    },
    choose(option) {
      this.$emit('update:modelValue', this.optionValue(option));
      this.$emit('change');
      this.open = false;
    },
    closeOnOutside(event) { if (!this.$el.contains(event.target)) this.open = false; },
    close() { this.open = false; },
    syncPopoverPosition() {
      if (!this.open) return;
      const trigger = this.$el.querySelector('.select-trigger');
      if (!trigger) return;
      const viewportPadding = 16;
      const preferredHeight = 248;
      const rect = trigger.getBoundingClientRect();
      const roomBelow = Math.max(0, window.innerHeight - rect.bottom - viewportPadding);
      const roomAbove = Math.max(0, rect.top - viewportPadding);
      this.openUpward = roomBelow < preferredHeight && roomAbove > roomBelow;
      this.popoverMaxHeight = Math.floor(Math.min(preferredHeight, this.openUpward ? roomAbove : roomBelow));
    },
  },
  template: `
    <div class="select-menu" :class="{ open, disabled, 'open-upward': openUpward }">
      <button v-bind="$attrs" class="select-trigger" type="button" :disabled="disabled"
        :aria-expanded="String(open)" aria-haspopup="listbox" @click="toggle" @keydown.esc="close">
        <span>{{ selectedLabel || placeholder }}</span><i aria-hidden="true"></i>
      </button>
      <div v-if="open" class="select-popover" role="listbox" :style="{ maxHeight: popoverMaxHeight + 'px' }">
        <button v-for="option in options" :key="optionValue(option)" type="button" role="option"
          :aria-selected="optionValue(option) === modelValue" :class="{ selected: optionValue(option) === modelValue }"
          @click="choose(option)">{{ optionLabel(option) }}</button>
      </div>
    </div>
  `,
};

createApp({
  data() {
    return {
      route: location.hash.slice(1) || '/resumes',
      navigation: [
        { route: '/resumes', code: 'CV', label: '简历管理' },
        { route: '/matches', code: 'AI', label: 'BOSS 岗位匹配' },
        { route: '/llm', code: 'KEY', label: 'LLM API Key' },
      ],
      llm: { base_url: '', model: '', key_configured: false, api_key_masked: '', tested_at: null, thinking_enabled: true, reasoning_effort: 'low' },
      llmForm: { base_url: '', model: '', api_key: '', thinking_enabled: true, reasoning_effort: 'low' },
      resumes: [], selectedResumeId: '', resultResumeId: '',
      monitorForm: { job_keyword: '', city: '北京', experience: '不限', degree: '不限', salary: '不限', pages: 2 }, monitorSaved: false,
      cityOptions: window.CITY_OPTIONS || [],
      experienceOptions: ['1-3年', '3-5年', '5-10年', '10年以上'],
      degreeOptions: ['大专', '本科', '硕士', '博士'],
      salaryOptions: ['10-20K', '20-50K', '50K以上'],
      pageOptions: Array.from({ length: 10 }, (_, index) => index + 1),
      reasoningOptions: ['low', 'high', 'max'],
      boss: { state: 'unknown', message: '' },
      matchStatus: { status: 'idle', stage: 'idle', progress_current: 0, progress_total: 0, message: '' },
      results: { new_published: [], new_active: [], collection_summary: null }, resultTab: 'new_published', runningResumeId: '',
      busy: { llm: false, upload: false, resume: false, boss: false, monitor: false },
      notice: { type: 'info', text: '' }, successDialog: { open: false, text: '' }, pollTimer: null,
    };
  },
  computed: {
    monitorStageLabel() {
      if (this.matchStatus.task !== 'monitor') return '';
      const labels = { checking: '检查中', scraping: '采集中', completed: '采集完成', failed: '执行失败' };
      return labels[this.matchStatus.stage] || '执行中';
    },
    monitorStageRunning() { return this.matchStatus.task === 'monitor' && this.matchStatus.status === 'running'; },
    matchStageLabel() {
      if (this.matchStatus.task !== 'match') return '';
      const labels = { scoring: '评分中', finalizing: '整理中', completed: '整理完成', failed: '匹配失败' };
      return labels[this.matchStatus.stage] || '匹配中';
    },
    matchStageRunning() { return this.matchStatus.task === 'match' && this.matchStatus.status === 'running'; },
    pageMeta() {
      return {
        '/resumes': { eyebrow: 'RESUME WORKBENCH', title: '简历管理', description: '上传文本型 PDF，检查 AI 摘要；解析成功的简历默认参与匹配。' },
        '/matches': { eyebrow: 'MANUAL MATCH RUN', title: 'BOSS 岗位匹配', description: '按这份简历的条件采集岗位，只对新发现或内容变化的职位进行评分。' },
        '/llm': { eyebrow: 'MODEL CONNECTION', title: 'LLM API Key', description: '' },
      }[this.route] || { eyebrow: '', title: '', description: '' };
    },
    selectedResume() { return this.resumes.find((item) => item.id === this.selectedResumeId); },
    resultResume() { return this.resumes.find((item) => item.id === this.resultResumeId); },
    eligibleResumes() { return this.resumes.filter((item) => item.status === 'ready'); },
    matchResumeOptions() { return this.eligibleResumes.map((resume) => ({ value: resume.id, label: resume.profile?.title || resume.filename })); },
    newActiveResults() {
      return [...(this.results.new_published || []), ...(this.results.new_active || [])]
        .filter((item) => this.jobActiveStatus(item) === '刚刚活跃');
    },
    currentResults() { return this.resultTab === 'new_active' ? this.newActiveResults : (this.results.new_published || []); },
    resultQuery() {
      const source = this.collectionSummary?.filters || this.results.query || this.results.conditions || this.results.search_conditions || {};
      return {
        keyword: source.keyword || source.job_keyword || this.monitorForm.job_keyword || '—',
        city: source.city || this.monitorForm.city || '北京（默认）',
        experience: source.experience || this.monitorForm.experience || '不限',
        degree: source.degree || this.monitorForm.degree || '不限',
        salary: source.salary || this.monitorForm.salary || '不限',
        pages: source.pages || this.monitorForm.pages || 2,
      };
    },
    collectionSummary() {
      const summary = this.results.collection_summary;
      return summary && typeof summary === 'object' && !Array.isArray(summary) ? summary : null;
    },
    collectionSummaryText() {
      const summary = this.collectionSummary;
      if (summary) return summary.formatted_summary || summary.formatted_text || summary.text || summary.summary || '';
      return typeof this.results.collection_summary === 'string' ? this.results.collection_summary : '';
    },
    selectedMatchIsEligible() { return this.eligibleResumes.some((resume) => resume.id === this.resultResumeId); },
    hasMatchedSelectedResume() {
      return Boolean(this.resultResumeId === this.results.resume_id && this.results.last_completed_at);
    },
    matchButtonLabel() { return this.hasMatchedSelectedResume ? '继续匹配' : '开始匹配'; },
    canSaveConditions() {
      return !this.busy.monitor && Boolean(this.monitorForm.job_keyword?.trim() && this.monitorForm.city?.trim());
    },
    canStartMonitor() {
      return this.matchStatus.status !== 'running' && this.monitorSaved && this.boss.state === 'ready';
    },
    canStartMatch() {
      return this.matchStatus.status !== 'running' && this.llm.tested_at && this.llm.key_configured
        && this.selectedMatchIsEligible;
    },
    taskLabel() { return ({ idle: '空闲', running: '运行中', completed: '已完成', failed: '失败' })[this.matchStatus.status] || '空闲'; },
    bossLabel() { return ({ unknown: '尚未检查', ready: '已就绪', login_required: '需要登录', platform_limited: '访问受限', chrome_unavailable: 'Chrome 不可用', collector_unavailable: '采集器不可用' })[this.boss.state] || this.boss.state; },
  },
  async mounted() {
    addEventListener('hashchange', this.onRouteChange);
    await Promise.all([this.loadLlm(), this.loadResumes(), this.loadMatchStatus(), this.loadMonitorSettings()]);
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
    showSuccess(text = '已保存') { this.successDialog = { open: true, text }; },
    async loadLlm() { const data = await this.api('/api/llm-settings'); if (data) { this.llm = data; this.llmForm = { base_url: data.base_url, model: data.model, api_key: '', thinking_enabled: data.thinking_enabled, reasoning_effort: data.reasoning_effort }; } },
    async saveLlm() {
      this.busy.llm = true;
      try {
        const payload = { base_url: this.llmForm.base_url, model: this.llmForm.model, thinking_enabled: this.llmForm.thinking_enabled, reasoning_effort: this.llmForm.reasoning_effort };
        if (this.llmForm.api_key) payload.api_key = this.llmForm.api_key;
        this.llm = await this.api('/api/llm-settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        this.llmForm.api_key = '';
        this.showSuccess('已保存');
      }
      catch (error) { this.flash(error.message, 'error'); } finally { this.busy.llm = false; }
    },
    async loadResumes() {
      this.resumes = await this.api('/api/resumes');
      if (!this.selectedResumeId && this.resumes.length) this.selectResume(this.resumes[0].id);
      if (this.resultResumeId && !this.eligibleResumes.some((resume) => resume.id === this.resultResumeId)) {
        this.resultResumeId = ''; this.results = { new_published: [], new_active: [], collection_summary: null };
      }
    },
    selectResume(id) { this.selectedResumeId = id; },
    async loadMonitorSettings() {
      try {
        const data = await this.api('/api/monitor-settings');
        if (data?.conditions) {
          this.monitorForm = { ...this.monitorForm, ...data.conditions };
          this.monitorSaved = true;
        }
      } catch (error) { this.flash(error.message, 'error'); }
    },
    async saveMonitorConditions() {
      this.busy.monitor = true;
      try {
        await this.api('/api/monitor-settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.monitorForm) });
        this.monitorSaved = true; this.showSuccess('监控条件已保存');
      } catch (error) { this.flash(error.message, 'error'); } finally { this.busy.monitor = false; }
    },
    async startMonitor() {
      if (!this.canStartMonitor) return;
      try {
        this.matchStatus = await this.api('/api/monitor', { method: 'POST' });
        this.beginPolling();
      } catch (error) { this.flash(error.message, 'error'); }
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
    async retryParse() { try { this.replaceResume(await this.api(`/api/resumes/${this.selectedResume.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ retry_parse: true }) })); } catch (error) { this.flash(error.message, 'error'); } },
    async deleteResume() {
      if (!this.selectedResume || !confirm(`删除 ${this.selectedResume.filename}？此操作无法恢复。`)) return;
      try { await this.api(`/api/resumes/${this.selectedResume.id}`, { method: 'DELETE' }); this.resumes = this.resumes.filter((item) => item.id !== this.selectedResumeId); this.selectedResumeId = ''; if (this.resumes.length) this.selectResume(this.resumes[0].id); this.showSuccess('简历已删除'); } catch (error) { this.flash(error.message, 'error'); }
    },
    replaceResume(updated) { const index = this.resumes.findIndex((item) => item.id === updated.id); if (index >= 0) this.resumes.splice(index, 1, updated); this.selectResume(updated.id); },
    async checkBoss() { this.busy.boss = true; try { this.boss = await this.api('/api/boss/status'); } catch (error) { this.flash(error.message, 'error'); } finally { this.busy.boss = false; } },
    async setupBoss() { this.busy.boss = true; try { this.boss = await this.api('/api/boss/setup', { method: 'POST' }); this.flash('已打开 BOSS 专用 Chrome，请手动登录后重新检查。'); } catch (error) { this.flash(error.message, 'error'); } finally { this.busy.boss = false; } },
    async startMatch() {
      if (!this.canStartMatch) return;
      try {
        this.matchStatus = await this.api('/api/matches', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resume_id: this.resultResumeId }),
        });
        this.runningResumeId = this.resultResumeId;
        this.beginPolling();
      } catch (error) { this.flash(error.message, 'error'); }
    },
    async loadMatchStatus() { this.matchStatus = await this.api('/api/matches/status'); },
    beginPolling() { this.stopPolling(); this.pollTimer = setInterval(async () => { try { await this.loadMatchStatus(); if (['completed', 'failed'].includes(this.matchStatus.status)) { this.stopPolling(); const finishedResumeId = this.runningResumeId || this.matchStatus.current_resume_id; if (this.matchStatus.task === 'match' && finishedResumeId) { this.resultResumeId = finishedResumeId; this.results = { new_published: [], new_active: [], collection_summary: null }; } this.runningResumeId = ''; await this.loadResumes(); if (this.matchStatus.status === 'completed' && this.matchStatus.message) this.flash(this.matchStatus.message, 'success'); if (this.matchStatus.status === 'failed') this.flash(this.matchStatus.message, 'error'); if (this.resultResumeId) await this.loadResults(); } } catch (error) { this.stopPolling(); this.flash('本地服务已断开，请重新双击启动。', 'error'); } }, 1000); },
    stopPolling() { if (this.pollTimer) clearInterval(this.pollTimer); this.pollTimer = null; },
    selectMatchResume() { this.results = { new_published: [], new_active: [], collection_summary: null }; this.loadResults(); },
    async loadResults() {
      if (!this.resultResumeId) { this.results = { new_published: [], new_active: [], collection_summary: null }; return; }
      try {
        this.results = await this.api(`/api/resumes/${this.resultResumeId}/results`);
      } catch (error) { this.flash(error.message, 'error'); }
    },
    optionsWithCurrent(options, value, key = '') {
      const hasCurrent = options.some((option) => (key ? option[key] : option) === value);
      return hasCurrent || !value ? options : [{ value, label: `当前值：${value}（请重新选择）` }, ...options];
    },
    resumeStatusLabel(status) { return ({ parsing: '解析中', ready: '已完成', parse_failed: '失败' })[status] || status; },
    formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; },
    join(values) { return values?.length ? values.join('、') : '—'; },
    formatSummaryItems(values) {
      if (!Array.isArray(values) || !values.length) return '—';
      return values.map((item) => {
        if (Array.isArray(item)) {
          const [name, count] = item;
          return count === undefined || count === null ? name : `${name} (${count})`;
        }
        if (item && typeof item === 'object') {
          const name = item.name ?? item.label ?? item.value;
          const count = item.count ?? item.value_count;
          return count === undefined || count === null ? name : `${name} (${count})`;
        }
        return item;
      }).filter(Boolean).join('、') || '—';
    },
    jobSummary(item) {
      const summary = item?.summary ?? item?.job_summary ?? item?.jd_summary;
      if (typeof summary === 'string') return summary.trim();
      if (Array.isArray(summary)) return summary.filter(Boolean).join('；');
      if (summary && typeof summary === 'object') {
        const text = summary.summary ?? summary.text ?? summary.content ?? summary.description;
        return typeof text === 'string' ? text.trim() : '';
      }
      return '';
    },
    jobActiveStatus(item) { return item?.boss_active_status || item?.active_status_raw || item?.active_status || ''; },
    detailStatus(item) {
      const status = item?.detail_status ?? item?.detail_page_status ?? item?.jd_status;
      if (status === true || ['success', 'completed', 'fetched', 'ready'].includes(String(status).toLowerCase())) return '详情 JD 已抓取';
      if (status === false || ['failed', 'error'].includes(String(status).toLowerCase())) return '详情抓取失败';
      if (['pending', 'running', 'fetching'].includes(String(status).toLowerCase())) return '详情抓取中';
      return this.jobSummary(item) || item?.jd_text ? '详情 JD 已抓取' : '暂无详情状态';
    },
    detailStatusClass(item) {
      const status = String(item?.detail_status ?? item?.detail_page_status ?? item?.jd_status ?? '').toLowerCase();
      if (status === false || ['failed', 'error'].includes(status)) return 'failed';
      if (['pending', 'running', 'fetching'].includes(status)) return 'pending';
      return this.jobSummary(item) || item?.jd_text || status === 'success' || status === 'completed' || status === 'fetched' || status === 'ready' ? 'ready' : 'unknown';
    },
  },
}).component('select-menu', SelectMenu).mount('#app');
