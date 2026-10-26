const API = '/api/v1';
const ANALYSIS_CACHE_KEY = 'sxsvn.analysis-cache.v1';
const state = {
  companies: [],
  overviewNews: [],
  news: [],
  selectedNews: null,
  analysis: null,
  analysisCache: {},
  activeAnalysisContext: null,
  selectionRequest: 0,
  activeJob: null,
  companyId: '',
  currentView: 'overview',
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value = '') => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const labels = {
  KEEP: 'Связано', REVIEW: 'Нужна проверка', DROP: 'Не связано',
  positive: 'Положительное', negative: 'Негативное', mixed: 'Смешанное', unknown: 'Не определено',
  direct: 'Прямое', indirect: 'Косвенное', short_term: 'Краткосрочный',
  medium_term: 'Среднесрочный', long_term: 'Долгосрочный', completed: 'Готово',
  partial: 'Частичный результат', failed: 'Ошибка', queued: 'В очереди', running: 'Выполняется',
};

const ocrLabels = { high: 'высокая', medium: 'средняя', low: 'низкая' };
const evidenceLabels = {
  fact: 'факт из новости', relationship: 'данные о компании', calculation: 'расчёт по платежам',
  user_statement: 'заявление пользователя', external_search: 'внешний источник',
  inference: 'вывод модели', hypothesis: 'гипотеза модели',
};

// ai_engine режет цитату на фиксированной длине — дорезаем до конца последнего слова.
function wordTrim(text, max = 300) {
  if (text.length < max) return text;
  const cut = text.slice(0, max).replace(/\s+\S*$/, '');
  return `${cut || text.slice(0, max)}…`;
}

// Страховка: модель иногда переносит в текст служебные поля и ID из входных данных
// («confirmed=false», «group_134402_sb»). Промпт ai_engine это запрещает, но при показе
// известные шаблоны всё равно заменяем на русские слова.
const technicalTerms = [
  [/\bconfirmed\s*=\s*false\b/gi, 'статус «не подтверждено»'],
  [/\bconfirmed\s*=\s*true\b/gi, 'статус «подтверждено»'],
  [/\bGROUP_ID\s+/g, ''],
  [/\bimpact_direction\s*=\s*unknown\b/gi, 'направление не определено'],
  [/\b(?:relevance_class|impact_direction|impact_horizon|impact_type)\s*=\s*\w+/gi, ''],
  [/\buser_statement\b/gi, 'заявление пользователя'],
  [/\bunknowns\b/gi, 'неизвестные'],
  [/\bconfidence\b/gi, 'уверенность'],
  [/(групп[а-яё]*)\s+group_\d+(?:_sb)?\b/gi, '$1'],  // «её группы group_…» -> «её группы»
  [/\bgroup_\d+(?:_sb)?\b/gi, 'группа компаний'],
  [/\b(?:news|analysis)_[0-9a-f]{8,}\b:?\s*/gi, ''],
  [/\bdf_\w+?_hack\.csv\b/gi, 'данные банка'],
  [/\b(?:prod_(?:loa|lea|gar|rko|dep|zpp)|FACTORING_MARK|CL_PRODUCT_\w+)\s*=\s*\d\b/gi, ''],
];
const technicalSkip = new Set(['analysis_id', 'news_id', 'company_id', 'ref_id', 'id', 'kind', 'type', 'url', 'status',
  'relevance_class', 'impact_direction', 'impact_horizon', 'impact_type', 'source_id', 'snippet_id']);

function humanText(text) {
  let out = text;
  for (const [pattern, replacement] of technicalTerms) out = out.replace(pattern, replacement);
  return out.replace(/\(\s*\)/g, '').replace(/\s{2,}/g, ' ').replace(/\s+([,.;:)])/g, '$1')
    .replace(/[,;]\s*([,.;)])/g, '$1').replace(/[,;:]\s*$/, '').trim();
}

// Под основанием показываем источник, только если это название («Альфа-Банк, «…»», «РБК»), а не ID.
function readableSource(value) {
  return value && /[а-яё]/i.test(value) && !/^(?:news|fact|analysis)_/i.test(value) ? value : '';
}

function humanizeResult(value, key = '') {
  if (typeof value === 'string') return technicalSkip.has(key) ? value : humanText(value);
  if (Array.isArray(value)) return value.map(item => humanizeResult(item, key));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, humanizeResult(v, k)]));
  }
  return value;
}

function label(value) {
  return labels[value] || value || 'Не указано';
}

function dateLabel(value, withTime = false) {
  if (!value) return 'Дата не указана';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ru-RU', withTime
    ? { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
    : { day: 'numeric', month: 'long', year: 'numeric' }).format(date);
}

function money(value) {
  if (value === null || value === undefined || value === '') return 'Нет данных';
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value) + ' ₽';
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === 'string' ? payload.detail : payload.detail?.message;
    throw new Error(detail || `Ошибка запроса (${response.status})`);
  }
  return payload;
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove('show'), 3400);
}

function setBusy(button, busy, busyText = 'Подождите…') {
  if (!button) return () => {};
  const previous = button.textContent;
  button.disabled = busy;
  if (busy) button.textContent = busyText;
  return () => { button.disabled = false; button.textContent = previous; };
}

function showView(name) {
  state.currentView = name;
  window.scrollTo(0, 0);
  state.selectionRequest += 1;
  $$('.page-view').forEach(view => view.classList.toggle('active', view.id === `view-${name}`));
  $$('.nav-item').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  const title = ({ overview: 'Обзор', news: 'События', company: 'Компания', reports: 'Аналитика банка' })[name];
  $('#view-title').textContent = title;
  if (name === 'company' && state.companyId) loadCompany(state.companyId);
  if (name === 'news') loadNews(true);
  if (name === 'overview' && state.selectedNews) selectNews(state.selectedNews.news_id);
}

function analysisKey(companyId, newsId) {
  return `${companyId}::${newsId}`;
}

function cachedAnalysis(companyId, newsId) {
  return state.analysisCache[analysisKey(companyId, newsId)] || null;
}

function rememberAnalysis(result, status, context = state.activeAnalysisContext) {
  if (!result || !context?.companyId || !context?.newsId) return;
  const key = analysisKey(context.companyId, context.newsId);
  state.analysisCache[key] = { result, status, savedAt: new Date().toISOString() };
  const recent = Object.entries(state.analysisCache)
    .sort(([, a], [, b]) => String(b.savedAt || '').localeCompare(String(a.savedAt || '')))
    .slice(0, 40);
  state.analysisCache = Object.fromEntries(recent);
  try { localStorage.setItem(ANALYSIS_CACHE_KEY, JSON.stringify(state.analysisCache)); } catch { /* session cache still works */ }
}

function restoreAnalysisCache() {
  try {
    const saved = JSON.parse(localStorage.getItem(ANALYSIS_CACHE_KEY) || '{}');
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
      state.analysisCache = Object.fromEntries(Object.entries(saved).filter(([, entry]) =>
        entry && typeof entry === 'object' && entry.result && typeof entry.result === 'object'
      ));
    }
  } catch { state.analysisCache = {}; }
}

function statusChip(name, ready) {
  const text = ready ? 'подключено' : 'не настроено';
  return `<span class="service-chip ${ready ? 'ready' : ''}"><i></i>${escapeHtml(name)} · ${text}</span>`;
}

async function loadHealth() {
  try {
    const health = await api('/health');
    const services = health.services || {};
    $('#service-strip').innerHTML = [
      statusChip('Профили и платежи', services.company_data),
      statusChip('Каталог новостей', services.news_catalog),
      statusChip('Отчёты банка', services.bank_reports),
      statusChip('Модель', services.llm),
    ].join('');
    const ready = services.company_data && services.news_catalog;
    $('#connection-dot').className = `connection-dot ${ready ? 'ready' : ''}`;
    $('#connection-label').textContent = ready ? 'Сервисы подключены' : 'Нужна подготовка данных';
    $('#connection-detail').textContent = services.llm ? '\u041c\u043e\u0434\u0435\u043b\u044c \u0430\u043d\u0430\u043b\u0438\u0437\u0430 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0430' : '\u0414\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u0440\u0435\u0437\u0435\u0440\u0432\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c';
  } catch (error) {
    $('#service-strip').innerHTML = statusChip('API', false);
    $('#connection-dot').className = 'connection-dot error';
    $('#connection-label').textContent = 'API недоступно';
    $('#connection-detail').textContent = error.message;
  }
}

async function loadCompanies(query = '') {
  const params = new URLSearchParams({ limit: '100' });
  if (query) params.set('query', query);
  const result = await api(`/companies?${params}`);
  state.companies = result.items || [];
  for (const select of [$('#company-select'), $('#company-select-inline')]) {
    if (!select) continue;
    const selected = state.companyId || select.value;
    select.innerHTML = '<option value="">Выберите компанию</option>' + state.companies.map(company => {
      const id = escapeHtml(company.company_id);
      const industry = company.industry ? ` · ${escapeHtml(company.industry)}` : '';
      // display_name — читаемая подпись из data_pipeline (данные обезличены); без неё — ID и отрасль.
      const text = company.display_name ? escapeHtml(company.display_name) : `${id}${industry}`;
      return `<option value="${id}">${text}</option>`;
    }).join('');
    select.value = selected;
  }
  if (!state.companies.length) {
    $('#company-select').innerHTML = '<option value="">Нет собранной базы компаний</option>';
    $('#company-select-inline').innerHTML = '<option value="">Нет собранной базы компаний</option>';
  }
  return state.companies;
}

function renderFeed(items, target, limit = Infinity) {
  const container = $(target);
  const visible = items.slice(0, limit);
  if (!visible.length) {
    container.innerHTML = '<div class="empty-inline">В каталоге пока нет событий. Можно добавить новость вручную или обновить RSS-источники.</div>';
    return;
  }
  container.innerHTML = visible.map(item => {
    const category = (item.prefilter || 'REVIEW').toLowerCase();
    const title = item.title || 'Без заголовка';
    const lead = item.lead || item.text_quality || '';
    const chosen = state.selectedNews?.news_id === item.news_id ? 'selected' : '';
    return `<button class="feed-card ${chosen}" data-news-id="${escapeHtml(item.news_id)}">
      <span class="feed-meta"><span>${escapeHtml(item.source_name || item.source_id || 'Источник')} · <span class="feed-date">${escapeHtml(dateLabel(item.published_at))}</span></span><span class="tag ${category}">${escapeHtml(label(item.prefilter || 'REVIEW'))}</span></span>
      <span class="feed-title">${escapeHtml(title)}</span>
      ${lead ? `<span class="feed-lead">${escapeHtml(lead)}</span>` : ''}
      <span class="feed-foot">${Number(item.sources_in_cluster || 1)} ${pluralSources(item.sources_in_cluster || 1)}${item.novelty?.length ? ` · ${escapeHtml(item.novelty.length)} новых факта` : ''}</span>
    </button>`;
  }).join('');
  $$('.feed-card', container).forEach(card => card.addEventListener('click', () => selectNews(card.dataset.newsId)));
}

function pluralSources(count) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return 'источник';
  if (mod10 >= 2 && mod10 <= 4 && !(mod100 >= 12 && mod100 <= 14)) return 'источника';
  return 'источников';
}

async function loadNews(full = false, query = null) {
  try {
    if (full) {
      const activeQuery = query ?? $('#full-news-search')?.value ?? '';
      let response;
      if (activeQuery.trim()) {
        const params = new URLSearchParams({ query: activeQuery.trim(), limit: '30' });
        const result = await api(`/news/search?${params}`);
        response = { items: result.data || [] };
      } else {
        const params = new URLSearchParams({ limit: '100' });
        ($('#full-relevance-filter')?.value || 'KEEP,REVIEW,DROP')
          .split(',').filter(Boolean).forEach(value => params.append('prefilter', value));
        response = await api(`/news?${params}`);
      }
      const allowed = ($('#full-relevance-filter')?.value || 'KEEP,REVIEW,DROP').split(',');
      state.news = (response.items || []).filter(item => allowed.includes(item.prefilter || 'REVIEW'));
      $('#full-news-count').textContent = `${state.news.length} \u0441\u043e\u0431\u044b\u0442\u0438\u0439`;
      renderFeed(state.news, '#full-feed');
      if (state.currentView === 'news' && state.selectedNews && !state.news.some(item => item.news_id === state.selectedNews.news_id)) {
        state.selectionRequest += 1;
        state.selectedNews = null;
        state.analysis = null;
        $('#news-detail').innerHTML = '<div class="detail-placeholder"><span class="detail-marker" aria-hidden="true">\u2190</span><p>\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0441\u043e\u0431\u044b\u0442\u0438\u0435 \u0438\u0437 \u0442\u0435\u043a\u0443\u0449\u0438\u0445 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u043e\u0432.</p></div>';
      }
      return;
    }

    const params = new URLSearchParams({ limit: '40' });
    ['KEEP', 'REVIEW'].forEach(value => params.append('prefilter', value));
    const response = await api(`/news?${params}`);
    state.overviewNews = response.items || [];
    $('#news-count').textContent = String(state.overviewNews.length);
    $('#home-service-count').textContent = String(state.overviewNews.length);
    renderFeed(state.overviewNews, '#overview-feed', 6);
  } catch (error) {
    const target = full ? '#full-feed' : '#overview-feed';
    $(target).innerHTML = `<div class="empty-inline">${escapeHtml(error.message)}</div>`;
  }
}

async function selectNews(newsId) {
  const item = [...state.overviewNews, ...state.news].find(news => news.news_id === newsId);
  if (!item) return;
  state.selectedNews = item;
  const requestId = ++state.selectionRequest;
  state.analysis = null;
  renderFeed(state.overviewNews, '#overview-feed', 6);
  renderFeed(state.news, '#full-feed');
  const detail = state.currentView === 'news' ? $('#news-detail') : $('#analysis-content');
  if (detail) {
    detail.classList.remove('hidden');
    $('#analysis-empty')?.classList.add('hidden');
    $('#overview-analysis')?.classList.add('has-content');
    detail.innerHTML = '<div class="loading-row">Загружаю публикацию…</div>';
    if (window.matchMedia('(max-width: 760px)').matches) requestAnimationFrame(() => detail.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  }
  try {
    const payload = await api(`/news/${encodeURIComponent(newsId)}`);
    if (requestId !== state.selectionRequest) return;
    state.selectedNews = { ...item, ...payload.news, ...payload.news.metadata, sources: payload.sources || [] };
    if (detail) {
      const saved = cachedAnalysis(state.companyId, newsId);
      if (saved) {
        state.analysis = saved.result;
        renderAnalysis(saved.result, saved.status || 'completed');
      } else {
        renderNewsDetail(payload, detail);
      }
    }
  } catch (error) {
    if (requestId !== state.selectionRequest) return;
    if (detail) detail.innerHTML = `<div class="empty-inline">${escapeHtml(error.message)}</div>`;
  }
}

function renderNewsDetail(payload, target) {
  $('#overview-analysis')?.classList.add('has-content');
  const news = payload.news;
  const sources = payload.sources || [];
  target.innerHTML = `<div class="selected-news-detail"><p class="eyebrow">СОБЫТИЕ · ${escapeHtml(label(news.metadata?.prefilter || state.selectedNews.prefilter))}</p>
    <h2 class="detail-title">${escapeHtml(news.title || 'Без заголовка')}</h2>
    <div class="feed-foot">${escapeHtml(state.selectedNews.source_name || news.source_id)} · ${escapeHtml(dateLabel(news.published_at))}</div>
    <p class="detail-body">${escapeHtml(news.text || news.lead || 'Полный текст публикации недоступен.')}</p>
    <h3 class="result-section">Первоисточники</h3>
    ${sources.map(source => `<div class="source-row"><span class="tag">${escapeHtml(source.source_name || source.source_id)}</span><span>${escapeHtml(dateLabel(source.published_at))}</span>${source.url ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">Открыть ↗</a>` : ''}</div>`).join('') || '<p class="empty-inline">Данные об источниках не найдены.</p>'}
    <div class="news-actions"><button class="button button-primary" data-analyze-selected>${state.companyId ? 'Разобрать для компании' : 'Сначала выберите компанию'}</button></div></div>`;
  const analyzeButton = $('[data-analyze-selected]', target);
  analyzeButton.disabled = !state.companyId;
  analyzeButton.addEventListener('click', runAnalysis);
}

function showAnalysisEmpty() {
  $('#analysis-empty').classList.remove('hidden');
  $('#analysis-content').classList.add('hidden');
  $('#overview-analysis')?.classList.remove('has-content');
}

function showAnalysisProgress(stage = 'Подготовка анализа', progress = 0) {
  if (state.currentView === 'news') {
    $('#news-detail').innerHTML = `<div class="job-progress"><span class="spinner"></span><span>${escapeHtml(stage)}</span></div><div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, Math.max(4, progress * 100))}%"></div></div><p class="empty-copy">Анализ события и его влияния на компанию.</p>`;
    return;
  }
  $('#analysis-empty').classList.add('hidden');
  const content = $('#analysis-content');
  content.classList.remove('hidden');
  content.innerHTML = `<div class="eyebrow">АНАЛИЗ СОБЫТИЯ</div><div class="job-progress"><span class="spinner"></span><span>${escapeHtml(stage)}</span></div><div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, Math.max(4, progress * 100))}%"></div></div><p class="empty-copy">Извлечение фактов и проверка оснований могут занять несколько минут.</p>`;
}

async function runAnalysis() {
  if (!state.companyId || !state.selectedNews) return;
  state.activeAnalysisContext = { companyId: state.companyId, newsId: state.selectedNews.news_id };
  showAnalysisProgress('Отправка новости в конвейер', 0.03);
  try {
    const job = await api('/analysis', {
      method: 'POST',
      body: JSON.stringify({ company_id: state.companyId, event_ids: [state.selectedNews.news_id] }),
    });
    state.activeJob = job.job_id;
    await pollJob(job.job_id, state.activeAnalysisContext);
  } catch (error) {
    renderJobError(error.message);
  }
}

async function pollJob(jobId, context = state.activeAnalysisContext) {
  for (let attempt = 0; attempt < 360; attempt += 1) {
    let job;
    try { job = await api(`/jobs/${encodeURIComponent(jobId)}`); }
    catch (error) { renderJobError(`Не удалось проверить статус задачи: ${error.message}`); return; }
    if (job.status === 'completed' || job.status === 'partial') {
      rememberAnalysis(job.result, job.status, context);
      if (state.companyId === context?.companyId && state.selectedNews?.news_id === context?.newsId) {
        state.analysis = job.result;
        renderAnalysis(job.result, job.status);
      }
      return;
    }
    if (job.status === 'failed') {
      renderJobError(job.error || 'Не удалось выполнить анализ');
      return;
    }
    showAnalysisProgress(job.stage || label(job.status), job.progress || 0);
    await new Promise(resolve => setTimeout(resolve, 1800));
  }
  renderJobError('Ожидание превысило лимит. Задача продолжает работать; обновите страницу статуса позже.');
}

function renderJobError(message) {
  if (state.currentView === 'news') {
    const target = $('#news-detail');
    target.innerHTML = `<div class="eyebrow">НЕ УДАЛОСЬ ЗАВЕРШИТЬ</div><p class="limitation">${escapeHtml(message)}</p><button class="button button-secondary" data-retry-analysis>Повторить запуск</button>`;
    $('[data-retry-analysis]', target).addEventListener('click', runAnalysis);
    return;
  }
  $('#analysis-empty').classList.add('hidden');
  const content = $('#analysis-content');
  content.classList.remove('hidden');
  content.innerHTML = `<div class="eyebrow">НЕ УДАЛОСЬ ЗАВЕРШИТЬ</div><p class="limitation">${escapeHtml(message)}</p><button class="button button-secondary" id="retry-analysis">Повторить запуск</button>`;
  $('#retry-analysis').addEventListener('click', runAnalysis);
}

function renderAnalysis(rawResult, status) {
  const result = humanizeResult(rawResult);
  if (state.currentView !== 'news') $('#analysis-empty').classList.add('hidden');
  const content = state.currentView === 'news' ? $('#news-detail') : $('#analysis-content');
  $('#overview-analysis')?.classList.add('has-content');
  content.classList.remove('hidden');
  const title = state.selectedNews?.title || state.selectedNews?.news_id || 'Анализ события';
  const events = result.events || [];
  const first = events[0] || {};
  const rationale = result.summary || first.mechanism || 'Анализ сохранён без краткого резюме.';
  const evidence = events.flatMap(event => event.evidence || []);
  // ТЗ: позицию Альфа-Банка показываем отдельно от выводов модели. Фрагменты отчётов
  // банка приходят из ai_engine с kind === 'analytics'; один фрагмент может быть
  // привязан к нескольким событиям — показываем его один раз.
  const bankEvidence = evidence
    .filter(item => item.kind === 'analytics')
    .filter((item, index, all) => all.findIndex(other => other.ref_id === item.ref_id) === index);
  const reasonEvidence = evidence.filter(item => item.kind !== 'analytics');
  // Ограничения приходят из трёх мест и часто повторяются (после уточнения одна мысль — до 4 раз).
  const limitations = [...(result.limitations || []), ...(result.missing_data || []), ...(first.limitations || [])]
    .filter((item, index, all) => item && all.findIndex(other => other.trim().toLowerCase() === item.trim().toLowerCase()) === index);
  const isDropOnly = events.length > 0 && events.every(event => event.relevance_class === 'DROP');
  const externalSources = (result.sources_used || [])
    .filter(source => source.type === 'external_search')
    .map(source => ({ title: source.title || source.id, url: source.id }));
  const articleSources = state.selectedNews?.sources || [];
  const sourceRows = [...articleSources, ...externalSources].filter((source, index, all) =>
    source.url && all.findIndex(item => item.url === source.url) === index
  );
  content.innerHTML = `<div class="analysis-topline"><div><p class="eyebrow">РЕЗУЛЬТАТ · ${escapeHtml(label(status))}</p><h2>${escapeHtml(title)}</h2></div><span class="tag ${status === 'partial' ? 'review' : 'keep'}">${escapeHtml(label(status))}</span></div>
    <p class="analysis-summary">${escapeHtml(rationale)}</p>
    <div class="analysis-metrics"><div class="analysis-metric"><small>РЕЛЕВАНТНОСТЬ</small><strong>${escapeHtml(label(first.relevance_class))}</strong></div><div class="analysis-metric"><small>НАПРАВЛЕНИЕ</small><strong>${escapeHtml(label(first.impact_direction))}</strong></div><div class="analysis-metric"><small>ГОРИЗОНТ</small><strong>${escapeHtml(label(first.impact_horizon))}</strong></div></div>
    ${events.map(event => `<section class="result-section"><h3>${escapeHtml(label(event.relevance_class))} · ${escapeHtml(label(event.impact_type))}</h3><div class="claim"><p>${escapeHtml(event.mechanism || 'Механизм влияния не подтверждён.')}</p>${(event.potential_consequences || []).map(item => `<p>${escapeHtml(item)}</p>`).join('')}${event.confidence !== null && event.confidence !== undefined ? `<small>Уверенность модели: ${Math.round(event.confidence * 100)}%</small>` : ''}</div></section>`).join('')}
    ${result.key_risks?.length ? `<section class="result-section"><h3>Риски</h3>${result.key_risks.map(item => `<div class="claim"><p>${escapeHtml(item)}</p></div>`).join('')}</section>` : ''}
    ${result.potential_opportunities?.length ? `<section class="result-section"><h3>Возможности</h3>${result.potential_opportunities.map(item => `<div class="claim"><p>${escapeHtml(item)}</p></div>`).join('')}</section>` : ''}
    ${bankEvidence.length ? `<section class="result-section bank-position"><h3>Позиция Альфа-Банка · ${bankEvidence.length}</h3><p class="bank-position-note">Цитаты из аналитических отчётов банка. Это позиция банка, а не вывод модели.</p>${bankEvidence.map(item => `<div class="claim"><p>${escapeHtml(wordTrim(item.quote || item.supports || ''))}</p><small>${escapeHtml(item.source_id || item.ref_id || 'Аналитика Альфа-Банка')}</small></div>`).join('')}</section>`
      : events.length && !isDropOnly ? `<section class="result-section bank-position"><h3>Позиция Альфа-Банка</h3><p class="bank-position-note">В аналитике банка материалов по теме не найдено. Вывод сделан без позиции банка.</p></section>` : ''}
    ${reasonEvidence.length ? `<section class="result-section"><h3>Основания вывода · ${reasonEvidence.length}</h3>${reasonEvidence.map(item => `<div class="claim"><p>${escapeHtml(item.quote || item.supports || 'Аналитическое основание')}</p><small>${escapeHtml([evidenceLabels[item.kind], readableSource(item.source_id)].filter(Boolean).join(' · '))}${item.confirmed === false ? ' · не подтверждено внешним источником' : ''}</small></div>`).join('')}</section>` : ''}
    ${sourceRows.length ? `<section class="result-section"><h3>Источники новости</h3>${sourceRows.map(source => {
      let safeUrl = false;
      try { safeUrl = ['http:', 'https:'].includes(new URL(source.url).protocol); } catch {}
      return safeUrl ? `<div class="source-row"><span>${escapeHtml(source.source_name || source.title || source.source || 'Источник')}</span><a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">Открыть ↗</a></div>` : '';
    }).join('')}</section>` : ''}
    ${limitations.length ? `<section class="result-section"><h3>Ограничения и пробелы в данных · ${limitations.length}</h3>${limitations.map(item => `<div class="limitation">${escapeHtml(item)}</div>`).join('')}</section>` : ''}
    ${result.recommended_checks?.length ? `<section class="result-section"><h3>Что проверить</h3>${result.recommended_checks.map(item => `<div class="claim"><p>${escapeHtml(item)}</p></div>`).join('')}</section>` : ''}
    <div class="news-actions"><button class="button button-secondary" id="show-refinement">Добавить уточнение</button></div>
    <form id="refinement-form" class="stack-form hidden"><label>Уточнение пользователя<textarea name="text" required minlength="3" rows="3" placeholder="Например: кредитный портфель полностью зафиксирован до 2028 года"></textarea></label><label>Что уточняем<select name="kind"><option value="dependency">Зависимость</option><option value="business_model">Бизнес-модель</option><option value="product">Продукт</option><option value="region">Регион</option><option value="other">Другое</option></select></label><button class="button button-primary" type="submit">Пересчитать анализ</button></form>`;
  $('#show-refinement', content).addEventListener('click', () => $('#refinement-form', content).classList.toggle('hidden'));
  $('#refinement-form', content).addEventListener('submit', submitRefinement);
}

async function submitRefinement(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const button = $('button[type="submit"]', form);
  const done = setBusy(button, true, 'Пересчёт…');
  state.activeAnalysisContext = { companyId: state.companyId, newsId: state.selectedNews?.news_id };
  showAnalysisProgress('Сохранение уточнения пользователя', 0.1);
  try {
    const queued = await api(`/analysis/${encodeURIComponent(state.analysis.analysis_id)}/refinements`, {
      method: 'POST',
      body: JSON.stringify({ text: data.get('text'), kind: data.get('kind'), affected_event_ids: [state.selectedNews.news_id] }),
    });
    done();
    state.activeJob = queued.job_id;
    await pollJob(queued.job_id);
  } catch (error) {
    done();
    renderJobError(error.message);
  }
}

async function loadCompany(companyId) {
  const container = $('#company-content');
  if (!companyId) {
    container.innerHTML = '<div class="panel empty-state">Выберите компанию из списка.</div>';
    return;
  }
  container.innerHTML = '<div class="panel loading-row">Загружаю профиль и платежные связи…</div>';
  try {
    const [profile, exposure, relationships] = await Promise.all([
      api(`/companies/${encodeURIComponent(companyId)}`),
      api(`/companies/${encodeURIComponent(companyId)}/exposure`),
      api(`/companies/${encodeURIComponent(companyId)}/relationships?limit=12`),
    ]);
    renderCompany(profile, exposure, relationships.items || []);
  } catch (error) {
    container.innerHTML = `<div class="panel empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function flattenProfile(profile) {
  return [
    ['ОТРАСЛЬ', profile.industry], ['ВИД ДЕЯТЕЛЬНОСТИ', profile.activities],
    ['РЕСУРСЫ', profile.key_resources], ['ФАКТОРЫ ЗАТРАТ', profile.cost_drivers],
    ['ГЕОГРАФИЯ', profile.geography],
  ].filter(([, value]) => Array.isArray(value) ? value.length : Boolean(value));
}

function renderCompany(profile, exposure, relationships) {
  const financials = profile.financials || {};
  const payments = financials.payments || {};
  const grouped = exposure.data || {};
  const sectors = [...(grouped.outgoing?.sectors || []), ...(grouped.incoming?.sectors || [])]
    .filter((sector, index, all) => all.findIndex(other => other.section_name === sector.section_name) === index)
    .sort((a, b) => (b.amount || 0) - (a.amount || 0)).slice(0, 7);
  const maxShare = Math.max(0.01, ...sectors.map(item => item.share || 0));
  const missing = profile.raw?.missing_data || [];
  $('#company-content').innerHTML = `<div class="company-top"><article class="panel company-card"><p class="company-id">\u041f\u0420\u041e\u0424\u0418\u041b\u042c \u041a\u041e\u041c\u041f\u0410\u041d\u0418\u0418</p><h2>${escapeHtml(profile.display_name || profile.raw?.display_name || profile.name || profile.company_id)}</h2><p>${escapeHtml(profile.industry || 'Отрасль не определена')}${profile.subindustry ? ` · ${escapeHtml(profile.subindustry)}` : ''}</p></article>
    <article class="panel stat-card"><small>ВЫРУЧКА</small><strong>${escapeHtml(money(financials.revenue?.value))}</strong><p>${escapeHtml(financials.revenue?.period || 'Период не указан')}</p></article>
    <article class="panel stat-card"><small>ВХОДЯЩИЕ ПОТОКИ</small><strong>${escapeHtml(formatCoverage(payments.incoming?.coverage))}</strong><p>${escapeHtml(payments.incoming?.counterparties ?? 'Нет данных')} контрагентов</p></article>
    <article class="panel stat-card"><small>ИСХОДЯЩИЕ ПОТОКИ</small><strong>${escapeHtml(formatCoverage(payments.outgoing?.coverage))}</strong><p>${escapeHtml(payments.outgoing?.counterparties ?? 'Нет данных')} контрагентов</p></article></div>
    <div class="company-columns"><section class="panel data-panel"><h2>Профиль</h2><div class="field-list">${flattenProfile(profile).map(([name, value]) => `<div class="field-row"><small>${name}</small><span>${escapeHtml(Array.isArray(value) ? value.join(' · ') : value)}</span></div>`).join('') || '<p class="empty-inline">Поля профиля отсутствуют.</p>'}</div>${missing.length ? `<div class="limitation"><strong>Пробелы в данных:</strong> ${escapeHtml(missing.slice(0, 5).join('; '))}${missing.length > 5 ? ` и ещё ${missing.length - 5}` : ''}</div>` : ''}</section>
    <section class="panel data-panel"><h2>Платёжные потоки по отраслям</h2>${sectors.length ? `<div class="bar-list">${sectors.map(sector => `<div class="bar-item"><span title="${escapeHtml(sector.section_name)}">${escapeHtml(sector.section_name)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (sector.share || 0) / maxShare * 100)}%"></div></div><strong>${formatCoverage(sector.share)}</strong></div>`).join('')}</div><div class="limitation">Доля платежей за период выгрузки, не доля выручки и не вероятность убытка. Неизвестные отрасли отражены отдельно.</div>` : '<p class="empty-inline">Платёжная структура недоступна.</p>'}</section></div>
    <section class="panel data-panel"><h2>Связи и контрагенты <span class="tag review">Наблюдения, не подтверждённая зависимость</span></h2>${relationships.length ? relationships.slice(0, 8).map(item => `<div class="source-row"><span class="tag">${escapeHtml(item.metadata?.label || item.relation_type || 'связь')}</span><strong>${escapeHtml(item.counterparty_name || item.counterparty_id)}</strong><span>${item.metadata?.label || item.direction === 'none' ? '' : escapeHtml(item.direction || '')}</span><span>${item.share == null ? '' : formatCoverage(item.share)}</span><span>${item.confirmed ? 'Подтверждено' : 'Не подтверждено'}</span></div>`).join('') : '<p class="empty-inline">Связи не найдены.</p>'}</section>`;
}

function formatCoverage(value) {
  return value == null ? 'н/д' : `${(Number(value) * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
}

async function searchReports() {
  const input = $('#report-query');
  const query = input.value.trim();
  if (query.length < 2) {
    toast('Введите не менее двух символов');
    return;
  }
  const button = $('#search-reports');
  const done = setBusy(button, true, 'Поиск…');
  $('#report-results').innerHTML = '<div class="loading-row">Ищу по страницам отчётов…</div>';
  try {
    const result = await api(`/reports/search?${new URLSearchParams({ query, limit: '8' })}`);
    renderReports(result.items || []);
  } catch (error) {
    $('#report-results').innerHTML = `<div class="empty-inline">${escapeHtml(error.message)}</div>`;
  } finally {
    done();
  }
}

function renderReports(items) {
  $('#report-count').textContent = `\u041d\u0430\u0439\u0434\u0435\u043d\u043e: ${items.length}`;
  if (!items.length) {
    $('#report-results').innerHTML = '<div class="report-initial"><span>—</span><p>Фрагменты по запросу не найдены.</p></div>';
    return;
  }
  $('#report-results').innerHTML = items.map(item => {
    const meta = item.metadata || {};
    return `<article class="report-card"><div class="report-card-top"><span class="tag">${escapeHtml(meta.report_date || 'Дата не указана')}</span><span>${escapeHtml(meta.period || '')}</span>${meta.stale ? '<span class="tag review">Исторический материал</span>' : ''}${meta.ocr_confidence ? `<span class="tag">Точность OCR: ${escapeHtml(ocrLabels[meta.ocr_confidence] || meta.ocr_confidence)}</span>` : ''}</div><h2>${escapeHtml(item.title)}</h2><blockquote>${escapeHtml(item.text)}</blockquote><div class="report-card-foot"><span>${escapeHtml(item.source)}</span><span>Страница ${escapeHtml(meta.page ?? '—')} · релевантность ${item.score == null ? '—' : escapeHtml(Number(item.score).toFixed(2).replace('.', ','))}</span></div>${(meta.notes || []).map(note => `<div class="limitation">${escapeHtml(note)}</div>`).join('')}</article>`;
  }).join('');
}

async function refreshSources() {
  const button = $('#refresh-news');
  const done = setBusy(button, true, 'Получаю ленты…');
  toast('Обновление RSS-источников началось');
  try {
    const result = await api('/news/fetch', { method: 'POST', body: JSON.stringify({ max_items: 30 }) });
    await loadNews();
    await loadHealth();
    const totals = result.totals || {};
    const failedSources = (result.sources || []).filter(source => ['error', 'partial'].includes(source.status));
    toast(failedSources.length
      ? `Лента обновлена: новых ${totals.new || 0}. Проверьте источники: ${failedSources.map(source => source.source_id).join(', ')}`
      : `Обновление завершено: новых ${totals.new || 0}, без изменений ${totals.unchanged || 0}`);
  } catch (error) {
    toast(error.message);
  } finally {
    done();
  }
}
function openAddNews() {
  $('#add-news-error').textContent = '';
  $('#add-news-dialog').showModal();
}

async function submitNews(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  if (!values.url) delete values.url;
  if (!values.published_at) delete values.published_at;
  const button = $('button[type="submit"]', form);
  const done = setBusy(button, true, 'Сохраняю…');
  try {
    const result = await api('/news', { method: 'POST', body: JSON.stringify(values) });
    $('#add-news-dialog').close();
    form.reset();
    toast(result.status === 'duplicate' ? 'Публикация сохранена как повторная' : 'Новость добавлена в каталог');
    await loadNews(true);
    const added = state.news.find(item => item.news_id === result.news_id);
    if (added) await selectNews(added.news_id);
  } catch (error) {
    $('#add-news-error').textContent = error.message;
  } finally {
    done();
  }
}

function wireEvents() {
  $$('.nav-item').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
  $$('[data-go="news"]').forEach(button => button.addEventListener('click', event => { event.preventDefault(); showView('news'); }));
  $$('[data-go="overview"]').forEach(button => button.addEventListener('click', event => { event.preventDefault(); showView('overview'); }));
  $('#company-select').addEventListener('change', event => {
    state.companyId = event.target.value;
    $('#company-select-inline').value = state.companyId;
    if (state.companyId) loadCompany(state.companyId);
    renderFeed(state.overviewNews, '#overview-feed', 6);
    if (state.selectedNews) selectNews(state.selectedNews.news_id);
  });
  $('#company-select-inline').addEventListener('change', event => {
    $('#company-select').value = event.target.value;
    $('#company-select').dispatchEvent(new Event('change'));
  });
  $('#full-relevance-filter').addEventListener('change', () => loadNews(true));
  let searchTimer;
  $('#full-news-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadNews(true), 280);
  });
  $('#search-reports').addEventListener('click', searchReports);
  $('#report-query').addEventListener('keydown', event => { if (event.key === 'Enter') searchReports(); });
  $('#refresh-news').addEventListener('click', refreshSources);
  $('#open-add-news').addEventListener('click', openAddNews);
  $('#add-news-form').addEventListener('submit', submitNews);
  $('#add-news-dialog').addEventListener('click', event => {
    if (event.target === $('#add-news-dialog')) $('#add-news-dialog').close();
  });
}

async function init() {
  $('#today').textContent = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date());
  restoreAnalysisCache();
  wireEvents();
  await Promise.all([loadHealth(), loadNews()]);
  try {
    const companies = await loadCompanies();
    if (companies.length) {
      state.companyId = companies[0].company_id;
      $('#company-select').value = state.companyId;
      $('#company-select-inline').value = state.companyId;
    }
  } catch (error) {
    $('#company-select').innerHTML = `<option value="">${escapeHtml(error.message)}</option>`;
    $('#company-select-inline').innerHTML = `<option value="">${escapeHtml(error.message)}</option>`;
  }
}

init();
