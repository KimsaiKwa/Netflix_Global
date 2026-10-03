// ==UserScript==
// @name         豆瓣影视热榜 · 中国 + 全球/地区
// @namespace    https://movie.douban.com/
// @version      1.6.0
// @description  豆瓣个人电影主页右侧热榜：中国豆瓣热榜 + Netflix 全球/美国/韩国/日本/欧洲周榜。Netflix 元数据由 GitHub 后台预处理。
// @match        https://movie.douban.com/mine*
// @match        https://movie.douban.com/people/*
// @run-at       document-idle
// @noframes
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      m.douban.com
// @connect      movie.douban.com
// @connect      raw.githubusercontent.com
// ==/UserScript==

(() => {
  'use strict';

  const VERSION = '1.6.0';
  const ROOT_ID = 'db-stream-rank-v160';
  const STYLE_ID = ROOT_ID + '-style';
  const STORE = 'db-stream-rank:v160:';
  const LEGACY_META_STORE = 'db-stream-meta:v1:';

  const CACHE_TTL = 3 * 60 * 60 * 1000;
  const WEEK_REF_TTL = 10 * 60 * 1000;
  const REFRESH_THROTTLE = 60 * 1000;

  const OWN_REPO_RAW =
    'https://raw.githubusercontent.com/KimsaiKwa/Netflix_Global/main';

  const URLS = {
    chinaMovie:
      'https://m.douban.com/rexxar/api/v2/subject/recent_hot/movie?start=0&limit=10&category=%E7%83%AD%E9%97%A8&type=%E5%8D%8E%E8%AF%AD',

    chinaTv:
      'https://m.douban.com/rexxar/api/v2/subject/recent_hot/tv?start=0&limit=10&category=tv&type=tv_domestic',

    global:
      OWN_REPO_RAW + '/global.json',

    country: code =>
      OWN_REPO_RAW + '/countries/' + code + '.json',
  };

  const EUROPE = [
    ['gb', '英国'],
    ['fr', '法国'],
    ['de', '德国'],
    ['es', '西班牙'],
    ['it', '意大利'],
    ['nl', '荷兰'],
    ['pl', '波兰'],
    ['se', '瑞典'],
    ['no', '挪威'],
    ['dk', '丹麦'],
    ['fi', '芬兰'],
    ['be', '比利时'],
    ['at', '奥地利'],
    ['ch', '瑞士'],
    ['pt', '葡萄牙'],
    ['ie', '爱尔兰'],
    ['cz', '捷克'],
    ['gr', '希腊'],
    ['hu', '匈牙利'],
    ['ro', '罗马尼亚'],
  ];

  const REGION_LABELS = {
    china: '中国',
    global: '全球',
    us: '美国',
    kr: '韩国',
    jp: '日本',
    europe: '欧洲',
  };

  const state = {
    region: migrateRegionPreference(),
    type: migrateTypePreference(),
    current: null,
    weekConsistency: null,
    requestSerial: 0,
    lastManualRefreshAt: 0,
    diagnostics: {},
  };

  if (!Object.prototype.hasOwnProperty.call(REGION_LABELS, state.region)) {
    state.region = 'china';
  }

  if (!['movie', 'tv'].includes(state.type)) {
    state.type = 'movie';
  }

  function clean(value, max = 500) {
    return String(value ?? '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, max);
  }

  function esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function normalizeText(value) {
    return String(value ?? '')
      .normalize('NFKC')
      .toLowerCase()
      .replace(/[’‘]/g, "'")
      .replace(/&/g, ' and ')
      .replace(/[^\p{L}\p{N}]+/gu, ' ')
      .trim();
  }

  function normalizeImageURL(value) {
    const url = clean(value, 1600);

    if (!url) {
      return '';
    }

    if (url.startsWith('//')) {
      return 'https:' + url;
    }

    return url.replace(/^http:\/\//i, 'https://');
  }

  function uniqueStrings(values) {
    const seen = new Set();
    const output = [];

    for (const value of values || []) {
      const text = clean(value, 1600);

      if (!text || seen.has(text)) {
        continue;
      }

      seen.add(text);
      output.push(text);
    }

    return output;
  }

  function parseYear(value) {
    const match =
      String(value || '').match(/(?:19|20)\d{2}/);

    return match ? match[0] : '';
  }

  function formatViews(value) {
    const n = Number(value);

    if (!Number.isFinite(n) || n <= 0) {
      return '';
    }

    if (n >= 1_000_000) {
      return (
        (n / 1_000_000)
          .toFixed(1)
          .replace(/\.0$/, '') +
        'M views'
      );
    }

    if (n >= 1000) {
      return Math.round(n / 1000) + 'K views';
    }

    return n + ' views';
  }

  function shortSeason(value, title = '') {
    let text = clean(value, 220);

    if (!text) {
      return '';
    }

    const prefix = clean(title, 220);

    if (
      prefix &&
      text
        .toLowerCase()
        .startsWith(prefix.toLowerCase() + ':')
    ) {
      text =
        clean(
          text.slice(prefix.length + 1),
          160
        );
    }

    return text;
  }

  function loadPref(key, fallback) {
    try {
      return GM_getValue(
        STORE + 'pref:' + key,
        fallback
      );
    } catch {
      return fallback;
    }
  }

  function savePref(key, value) {
    try {
      GM_setValue(
        STORE + 'pref:' + key,
        value
      );
    } catch {}
  }

  function loadJSON(key, fallback = null) {
    try {
      const raw =
        GM_getValue(
          STORE + key,
          ''
        );

      if (!raw) {
        return fallback;
      }

      return typeof raw === 'string'
        ? JSON.parse(raw)
        : raw;
    } catch {
      return fallback;
    }
  }

  function saveJSON(key, value) {
    try {
      GM_setValue(
        STORE + key,
        JSON.stringify(value)
      );
    } catch {}
  }

  function migrateRegionPreference() {
    const oldStores = [
      STORE,
      'db-stream-rank:v150:',
      'db-stream-rank:v140:',
      'db-stream-rank:v131:',
      'db-stream-rank:v130:',
      'db-stream-rank:v120:',
      'db-stream-rank:v110:',
      'db-stream-rank:v100:',
    ];

    for (const prefix of oldStores) {
      try {
        const region =
          GM_getValue(
            prefix + 'pref:region',
            ''
          );

        if (
          Object.prototype.hasOwnProperty.call(
            REGION_LABELS,
            region
          )
        ) {
          return region;
        }

        const platform =
          GM_getValue(
            prefix + 'pref:platform',
            ''
          );

        const market =
          GM_getValue(
            prefix + 'pref:market',
            'global'
          );

        if (platform === 'china') {
          return 'china';
        }

        if (
          ['global', 'us', 'kr', 'jp', 'europe']
            .includes(market)
        ) {
          return market;
        }
      } catch {}
    }

    return 'china';
  }

  function migrateTypePreference() {
    const oldStores = [
      STORE,
      'db-stream-rank:v150:',
      'db-stream-rank:v140:',
      'db-stream-rank:v131:',
      'db-stream-rank:v130:',
      'db-stream-rank:v120:',
      'db-stream-rank:v110:',
      'db-stream-rank:v100:',
    ];

    for (const prefix of oldStores) {
      try {
        const type =
          GM_getValue(
            prefix + 'pref:type',
            ''
          );

        if (['movie', 'tv'].includes(type)) {
          return type;
        }
      } catch {}
    }

    return 'movie';
  }

  function requestJSON(url, options = {}) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method:
          options.method || 'GET',

        url,

        timeout:
          options.timeout || 30000,

        anonymous:
          options.anonymous ?? true,

        headers:
          options.headers || {
            Accept:
              'application/json,text/plain,*/*',

            'User-Agent':
              'Mozilla/5.0',
          },

        onload(response) {
          if (
            response.status < 200 ||
            response.status >= 300
          ) {
            reject(
              new Error(
                'HTTP ' + response.status
              )
            );
            return;
          }

          try {
            resolve(
              JSON.parse(
                response.responseText || 'null'
              )
            );
          } catch (error) {
            reject(
              new Error(
                'JSON解析失败：' +
                error.message
              )
            );
          }
        },

        onerror() {
          reject(
            new Error('网络请求失败')
          );
        },

        ontimeout() {
          reject(
            new Error('请求超时')
          );
        },
      });
    });
  }

  async function mapLimit(items, limit, worker) {
    const results = new Array(items.length);
    let cursor = 0;

    async function runner() {
      while (true) {
        const index = cursor++;

        if (index >= items.length) {
          return;
        }

        results[index] =
          await worker(
            items[index],
            index
          );
      }
    }

    await Promise.all(
      Array.from(
        {
          length:
            Math.min(
              limit,
              items.length
            ),
        },
        () => runner()
      )
    );

    return results;
  }

  async function cachedFetch(
    key,
    fetcher,
    force = false
  ) {
    const cacheKey =
      'cache:' + key;

    const cached =
      loadJSON(
        cacheKey,
        null
      );

    const now =
      Date.now();

    if (
      !force &&
      cached?.savedAt &&
      now - cached.savedAt < CACHE_TTL &&
      cached?.payload
    ) {
      return {
        payload: cached.payload,
        cacheState: 'fresh',
        cachedAt: cached.savedAt,
      };
    }

    try {
      const payload =
        await fetcher();

      saveJSON(
        cacheKey,
        {
          savedAt: now,
          payload,
        }
      );

      return {
        payload,
        cacheState: 'network',
        cachedAt: now,
      };
    } catch (error) {
      if (cached?.payload) {
        return {
          payload: cached.payload,
          cacheState: 'stale',
          cachedAt:
            cached.savedAt || null,
          error:
            error.message ||
            String(error),
        };
      }

      throw error;
    }
  }

  /*
   * Transition only:
   * old v1.3-v1.5 local metadata cache can fill gaps if a backend-enriched
   * JSON has not reached GitHub yet. It performs no network requests.
   */
  function loadLegacyMeta(title, mediaType) {
    const key =
      mediaType +
      ':' +
      normalizeText(title);

    try {
      const raw =
        GM_getValue(
          LEGACY_META_STORE + key,
          ''
        );

      if (!raw) {
        return {};
      }

      const record =
        typeof raw === 'string'
          ? JSON.parse(raw)
          : raw;

      const payload =
        record?.payload || {};

      const cn =
        clean(
          payload.cnTitle ||
          payload.cn_title,
          220
        );

      return {
        cnTitle:
          cn === '暂无中文译名'
            ? ''
            : cn,

        year:
          clean(
            payload.year,
            8
          ),

        posterCandidates:
          uniqueStrings([
            ...(
              payload.posterCandidates ||
              payload.poster_candidates ||
              []
            ),
            payload.doubanPoster,
            payload.douban_poster,
            payload.justWatchPoster,
            payload.justwatch_poster,
            payload.poster,
          ]),

        doubanId:
          clean(
            payload.doubanId ||
            payload.douban_id,
            40
          ),

        imdbId:
          clean(
            payload.imdbId ||
            payload.imdb_id,
            40
          ),

        tmdbId:
          clean(
            payload.tmdbId ||
            payload.tmdb_id,
            40
          ),
      };
    } catch {
      return {};
    }
  }

  function metadataFromRow(
    row,
    mediaType
  ) {
    const legacy =
      loadLegacyMeta(
        row.title,
        mediaType
      );

    const backendPosters =
      uniqueStrings([
        ...(
          Array.isArray(
            row.poster_candidates
          )
            ? row.poster_candidates
            : []
        ),
        row.poster,
      ]);

    return {
      cnTitle:
        clean(
          row.cn_title,
          220
        ) ||
        legacy.cnTitle ||
        '',

      year:
        clean(
          row.year,
          8
        ) ||
        legacy.year ||
        '',

      posterCandidates:
        uniqueStrings([
          ...backendPosters,
          ...(
            legacy.posterCandidates ||
            []
          ),
        ]),

      doubanId:
        clean(
          row.douban_id,
          40
        ) ||
        legacy.doubanId ||
        '',

      imdbId:
        clean(
          row.imdb_id,
          40
        ) ||
        legacy.imdbId ||
        '',

      tmdbId:
        clean(
          row.tmdb_id,
          40
        ) ||
        legacy.tmdbId ||
        '',
    };
  }

  /*
   * China ranking remains live Douban recent_hot.
   */
  function normalizeChinaItem(raw, index) {
    const id =
      raw.id ||
      raw.subject_id ||
      raw.target?.id ||
      '';

    const pic =
      raw.pic ||
      raw.cover ||
      raw.target?.pic ||
      {};

    const rating =
      raw.rating ||
      raw.target?.rating ||
      {};

    const subtitle =
      clean(
        raw.card_subtitle ||
        raw.subtitle ||
        raw.target?.card_subtitle,
        240
      );

    const year =
      clean(
        raw.year ||
        raw.target?.year ||
        parseYear(subtitle),
        8
      );

    const ratingValue =
      Number(
        rating.value ||
        rating.score ||
        0
      );

    const poster =
      normalizeImageURL(
        pic.normal ||
        pic.large ||
        pic.url ||
        raw.cover_url ||
        ''
      );

    return {
      rank: index + 1,

      title:
        clean(
          raw.title ||
          raw.target?.title ||
          '未命名',
          220
        ),

      cnTitle: '',
      season: '',
      year,
      poster,
      posterCandidates:
        poster ? [poster] : [],

      url:
        id
          ? 'https://movie.douban.com/subject/' +
            id +
            '/'
          : clean(
              raw.url,
              1000
            ),

      meta:
        [
          year,

          ratingValue > 0
            ? ratingValue.toFixed(1) +
              '分'
            : '',
        ]
          .filter(Boolean)
          .join(' · ') ||
        subtitle,
    };
  }

  async function fetchChina(type) {
    const isMovie =
      type === 'movie';

    const url =
      isMovie
        ? URLS.chinaMovie
        : URLS.chinaTv;

    const json =
      await requestJSON(
        url,
        {
          anonymous: false,

          headers: {
            Accept:
              'application/json, text/plain, */*',

            Referer:
              isMovie
                ? 'https://movie.douban.com/explore'
                : 'https://movie.douban.com/tv/',

            Origin:
              'https://movie.douban.com',

            'User-Agent':
              'Mozilla/5.0',
          },
        }
      );

    const rows =
      Array.isArray(json?.items)
        ? json.items
        : Array.isArray(
            json?.subject_collection_items
          )
          ? json.subject_collection_items
          : [];

    if (rows.length < 10) {
      throw new Error(
        '豆瓣返回数量不足：' +
        rows.length
      );
    }

    return {
      week: '',

      source:
        isMovie
          ? '豆瓣最近热门 · 华语电影'
          : '豆瓣最近热门 · 国内剧集',

      methodology:
        '豆瓣 recent_hot',

      metadataMode:
        'douban-native',

      items:
        rows
          .slice(0, 10)
          .map(normalizeChinaItem),
    };
  }

  function normalizeExternalRow(
    row,
    index,
    mediaType
  ) {
    const meta =
      metadataFromRow(
        row,
        mediaType
      );

    return {
      rank:
        Number(row.rank) ||
        index + 1,

      title:
        clean(
          row.title,
          220
        ),

      cnTitle:
        meta.cnTitle,

      season:
        clean(
          row.season,
          220
        ),

      year:
        meta.year,

      poster:
        meta.posterCandidates[0] || '',

      posterCandidates:
        meta.posterCandidates,

      doubanId:
        meta.doubanId,

      imdbId:
        meta.imdbId,

      tmdbId:
        meta.tmdbId,

      views:
        Number(row.views) ||
        0,

      weeksInTop10:
        Number(
          row.weeks_in_top_10
        ) ||
        0,

      sourceCategory:
        clean(
          row.source_category,
          80
        ),
    };
  }

  async function fetchGlobal(type) {
    const json =
      await requestJSON(
        URLS.global,
        {
          anonymous: true,

          headers: {
            Accept:
              'application/json,text/plain,*/*',

            'User-Agent':
              'Mozilla/5.0',
          },
        }
      );

    const mediaType =
      type === 'movie'
        ? 'movie'
        : 'tv';

    const rows =
      type === 'movie'
        ? json?.films
        : json?.tv;

    if (
      !Array.isArray(rows) ||
      rows.length < 10
    ) {
      throw new Error(
        '全球榜数据不完整'
      );
    }

    return {
      week:
        clean(
          json.week,
          20
        ),

      source:
        'Tudum 全球周榜',

      methodology:
        '英语/非英语榜按 weekly_views 合并',

      dataHost:
        'KimsaiKwa/Netflix_Global',

      metadataVersion:
        json.metadata_version ??
        null,

      metadataEnrichedAt:
        clean(
          json.metadata_enriched_at,
          80
        ) ||
        null,

      metadataMode:
        json.metadata_version
          ? 'github-preenriched'
          : 'legacy-cache-fallback',

      items:
        rows
          .slice(0, 10)
          .map(
            (row, index) =>
              normalizeExternalRow(
                row,
                index,
                mediaType
              )
          ),
    };
  }

  async function fetchCountry(
    code,
    type
  ) {
    const json =
      await requestJSON(
        URLS.country(code),
        {
          anonymous: true,

          headers: {
            Accept:
              'application/json,text/plain,*/*',

            'User-Agent':
              'Mozilla/5.0',
          },
        }
      );

    const mediaType =
      type === 'movie'
        ? 'movie'
        : 'tv';

    const rows =
      type === 'movie'
        ? json?.films
        : json?.tv;

    if (
      !Array.isArray(rows) ||
      rows.length < 10
    ) {
      throw new Error(
        code.toUpperCase() +
        '榜数据不完整'
      );
    }

    return {
      week:
        clean(
          json.week,
          20
        ),

      source:
        'Tudum ' +
        (
          REGION_LABELS[code] ||
          clean(
            json.country_name ||
            code.toUpperCase(),
            80
          )
        ) +
        '周榜',

      methodology:
        'Tudum 国家 Top 10',

      dataHost:
        'KimsaiKwa/Netflix_Global',

      metadataVersion:
        json.metadata_version ??
        null,

      metadataEnrichedAt:
        clean(
          json.metadata_enriched_at,
          80
        ) ||
        null,

      metadataMode:
        json.metadata_version
          ? 'github-preenriched'
          : 'legacy-cache-fallback',

      items:
        rows
          .slice(0, 10)
          .map(
            (row, index) =>
              normalizeExternalRow(
                row,
                index,
                mediaType
              )
          ),
    };
  }

  function dominantWeek(datasets) {
    const counts = new Map();

    for (const item of datasets) {
      if (!item?.week) {
        continue;
      }

      counts.set(
        item.week,
        (
          counts.get(item.week) ||
          0
        ) + 1
      );
    }

    const sorted =
      [...counts.entries()]
        .sort((a, b) => {
          if (b[1] !== a[1]) {
            return b[1] - a[1];
          }

          return String(b[0])
            .localeCompare(
              String(a[0])
            );
        });

    return sorted[0]?.[0] || '';
  }

  function aggregateEurope(
    datasets,
    type
  ) {
    const map = new Map();

    for (const dataset of datasets) {
      const rows =
        type === 'movie'
          ? dataset.films
          : dataset.tv;

      for (const raw of rows || []) {
        const row =
          normalizeExternalRow(
            raw,
            Number(raw.rank) - 1,
            type
          );

        const title =
          clean(
            row.title,
            220
          );

        const season =
          type === 'tv'
            ? clean(
                row.season,
                220
              )
            : '';

        const rank =
          Number(row.rank);

        if (
          !title ||
          !Number.isFinite(rank) ||
          rank < 1 ||
          rank > 10
        ) {
          continue;
        }

        const key =
          normalizeText(title) +
          '|' +
          normalizeText(season);

        let item =
          map.get(key);

        if (!item) {
          item = {
            title,
            season,
            countries: [],
            totalPoints: 0,
            rankSum: 0,
            cnTitle:
              row.cnTitle || '',
            year:
              row.year || '',
            posterCandidates:
              row.posterCandidates || [],
            doubanId:
              row.doubanId || '',
            imdbId:
              row.imdbId || '',
            tmdbId:
              row.tmdbId || '',
          };

          map.set(key, item);
        } else {
          if (
            !item.cnTitle &&
            row.cnTitle
          ) {
            item.cnTitle =
              row.cnTitle;
          }

          if (
            !item.year &&
            row.year
          ) {
            item.year =
              row.year;
          }

          item.posterCandidates =
            uniqueStrings([
              ...item.posterCandidates,
              ...(
                row.posterCandidates ||
                []
              ),
            ]);

          item.doubanId =
            item.doubanId ||
            row.doubanId ||
            '';

          item.imdbId =
            item.imdbId ||
            row.imdbId ||
            '';

          item.tmdbId =
            item.tmdbId ||
            row.tmdbId ||
            '';
        }

        const points =
          11 - rank;

        item.totalPoints +=
          points;

        item.rankSum +=
          rank;

        item.countries.push({
          code:
            dataset.code.toUpperCase(),

          name:
            dataset.name,

          rank,
          points,
        });
      }
    }

    return [...map.values()]
      .map(item => ({
        ...item,

        countryCount:
          item.countries.length,

        averageRank:
          item.rankSum /
          item.countries.length,
      }))
      .sort((a, b) =>
        b.countryCount -
          a.countryCount ||
        b.totalPoints -
          a.totalPoints ||
        a.averageRank -
          b.averageRank ||
        a.title.localeCompare(
          b.title
        )
      )
      .slice(0, 10)
      .map((item, index) => ({
        rank: index + 1,
        title: item.title,
        cnTitle:
          item.cnTitle || '',
        season:
          item.season,
        year:
          item.year || '',
        poster:
          item.posterCandidates[0] ||
          '',
        posterCandidates:
          item.posterCandidates || [],
        doubanId:
          item.doubanId || '',
        imdbId:
          item.imdbId || '',
        tmdbId:
          item.tmdbId || '',
        views: 0,
        weeksInTop10: 0,
        countryCount:
          item.countryCount,
        totalPoints:
          item.totalPoints,
        averageRank:
          Number(
            item.averageRank.toFixed(2)
          ),
      }));
  }

  async function fetchEurope(type) {
    const results =
      await mapLimit(
        EUROPE,
        5,
        async ([code, name]) => {
          try {
            const json =
              await requestJSON(
                URLS.country(code),
                {
                  anonymous: true,
                  timeout: 20000,

                  headers: {
                    Accept:
                      'application/json,text/plain,*/*',

                    'User-Agent':
                      'Mozilla/5.0',
                  },
                }
              );

            if (
              !json?.week ||
              !Array.isArray(
                json?.films
              ) ||
              !Array.isArray(
                json?.tv
              )
            ) {
              throw new Error(
                '结构异常'
              );
            }

            return {
              ok: true,
              code,

              name:
                clean(
                  json.country_name ||
                  name,
                  80
                ),

              week:
                clean(
                  json.week,
                  20
                ),

              metadataVersion:
                json.metadata_version ??
                null,

              metadataEnrichedAt:
                clean(
                  json.metadata_enriched_at,
                  80
                ) ||
                null,

              films:
                json.films,

              tv:
                json.tv,
            };
          } catch (error) {
            return {
              ok: false,
              code,
              name,
              error:
                error.message ||
                String(error),
            };
          }
        }
      );

    const valid =
      results.filter(
        item => item.ok
      );

    const week =
      dominantWeek(valid);

    const sameWeek =
      valid.filter(
        item =>
          item.week === week
      );

    const weekDistribution = {};

    for (const item of valid) {
      weekDistribution[item.week] =
        (
          weekDistribution[item.week] ||
          0
        ) + 1;
    }

    const outOfWeekCountries =
      valid
        .filter(
          item =>
            item.week !== week
        )
        .map(item => ({
          code:
            item.code.toUpperCase(),

          week:
            item.week,
        }));

    if (
      !week ||
      sameWeek.length < 12
    ) {
      throw new Error(
        '欧洲同周有效国家不足：' +
        sameWeek.length +
        '/' +
        EUROPE.length
      );
    }

    const items =
      aggregateEurope(
        sameWeek,
        type
      );

    if (items.length < 10) {
      throw new Error(
        '欧洲综合榜数量不足：' +
        items.length
      );
    }

    const metadataVersions =
      [
        ...new Set(
          sameWeek
            .map(
              item =>
                item.metadataVersion
            )
            .filter(
              value =>
                value !== null &&
                value !== undefined
            )
        ),
      ];

    const metadataTimes =
      sameWeek
        .map(
          item =>
            item.metadataEnrichedAt
        )
        .filter(Boolean)
        .sort();

    return {
      week,

      source:
        'Tudum 欧洲综合周榜 · ' +
        sameWeek.length +
        '国',

      methodology:
        '先按入榜国家数，再按总积分，再按平均排名',

      dataHost:
        'KimsaiKwa/Netflix_Global',

      metadataVersion:
        metadataVersions.length === 1
          ? metadataVersions[0]
          : null,

      metadataEnrichedAt:
        metadataTimes.length
          ? metadataTimes[
              metadataTimes.length - 1
            ]
          : null,

      metadataMode:
        metadataVersions.length
          ? 'github-preenriched'
          : 'legacy-cache-fallback',

      validCountryCount:
        sameWeek.length,

      totalCountryResponses:
        valid.length,

      allCountriesAvailable:
        valid.length ===
        EUROPE.length,

      countryWeeksConsistent:
        sameWeek.length ===
        valid.length,

      weekDistribution,

      outOfWeekCountries,

      failedCountries:
        results
          .filter(
            item => !item.ok
          )
          .map(
            item =>
              item.code.toUpperCase()
          ),

      items,
    };
  }

  async function fetchDataset(
    force = false
  ) {
    if (state.region === 'china') {
      return cachedFetch(
        'china:' + state.type,
        () =>
          fetchChina(state.type),
        force
      );
    }

    if (state.region === 'global') {
      return cachedFetch(
        'global:' + state.type,
        () =>
          fetchGlobal(state.type),
        force
      );
    }

    if (state.region === 'europe') {
      return cachedFetch(
        'europe:' + state.type,
        () =>
          fetchEurope(state.type),
        force
      );
    }

    return cachedFetch(
      state.region +
      ':' +
      state.type,
      () =>
        fetchCountry(
          state.region,
          state.type
        ),
      force
    );
  }

  /*
   * Data-week consistency guard.
   */
  async function fetchReferenceWeek(
    force = false
  ) {
    const cacheKey =
      'week-reference';

    const cached =
      loadJSON(
        cacheKey,
        null
      );

    const now =
      Date.now();

    if (
      !force &&
      cached?.savedAt &&
      cached?.week &&
      now - cached.savedAt <
        WEEK_REF_TTL
    ) {
      return {
        week: cached.week,
        cacheState: 'fresh',
        cachedAt: cached.savedAt,
      };
    }

    const url =
      URLS.global +
      '?week_ref=' +
      now;

    const json =
      await requestJSON(
        url,
        {
          anonymous: true,
          timeout: 20000,

          headers: {
            Accept:
              'application/json,text/plain,*/*',

            'Cache-Control':
              'no-cache',

            'User-Agent':
              'Mozilla/5.0',
          },
        }
      );

    const week =
      clean(
        json?.week,
        20
      );

    if (!week) {
      throw new Error(
        'global.json 缺少 week'
      );
    }

    saveJSON(
      cacheKey,
      {
        savedAt: now,
        week,
      }
    );

    return {
      week,
      cacheState: 'network',
      cachedAt: now,
    };
  }

  async function assessWeekConsistency(
    payload,
    force = false
  ) {
    if (state.region === 'china') {
      return {
        applicable: false,
        dataWeek: null,
        referenceWeek: null,
        weekConsistent: null,
        syncStatus:
          'not_applicable',
        updating: false,
        syncReasons: [],
      };
    }

    const dataWeek =
      clean(
        payload?.week,
        20
      );

    if (state.region === 'global') {
      return {
        applicable: true,
        dataWeek,
        referenceWeek: dataWeek,
        weekConsistent:
          Boolean(dataWeek),
        syncStatus:
          dataWeek
            ? 'ok'
            : 'unknown',
        updating: false,
        syncReasons:
          dataWeek
            ? []
            : [
                '全球榜缺少数据周',
              ],
        referenceCacheState:
          'self',
      };
    }

    let reference = null;
    let referenceError = null;

    try {
      reference =
        await fetchReferenceWeek(
          force
        );
    } catch (error) {
      referenceError =
        error.message ||
        String(error);
    }

    if (
      reference?.week &&
      dataWeek &&
      reference.week !== dataWeek &&
      !force
    ) {
      try {
        reference =
          await fetchReferenceWeek(
            true
          );
        referenceError = null;
      } catch (error) {
        referenceError =
          error.message ||
          String(error);
      }
    }

    const referenceWeek =
      clean(
        reference?.week,
        20
      );

    const reasons = [];

    if (state.region === 'europe') {
      const outOfWeek =
        Array.isArray(
          payload?.outOfWeekCountries
        )
          ? payload.outOfWeekCountries
          : [];

      if (outOfWeek.length) {
        reasons.push(
          '欧洲有 ' +
          outOfWeek.length +
          ' 个国家数据周不同'
        );
      }
    }

    if (
      dataWeek &&
      referenceWeek &&
      dataWeek !== referenceWeek
    ) {
      reasons.push(
        '当前榜 ' +
        dataWeek +
        '，全球最新 ' +
        referenceWeek
      );
    }

    if (!referenceWeek) {
      return {
        applicable: true,
        dataWeek:
          dataWeek || null,
        referenceWeek: null,
        weekConsistent:
          reasons.length
            ? false
            : null,
        syncStatus:
          reasons.length
            ? 'updating'
            : 'unknown',
        updating:
          reasons.length > 0,
        syncReasons: reasons,
        referenceError,
        referenceCacheState:
          null,
        referenceCachedAt:
          null,
      };
    }

    const consistent =
      reasons.length === 0;

    return {
      applicable: true,
      dataWeek:
        dataWeek || null,
      referenceWeek,
      weekConsistent:
        consistent,
      syncStatus:
        consistent
          ? 'ok'
          : 'updating',
      updating:
        !consistent,
      syncReasons: reasons,
      referenceError,
      referenceCacheState:
        reference?.cacheState ||
        null,
      referenceCachedAt:
        reference?.cachedAt
          ? new Date(
              reference.cachedAt
            ).toISOString()
          : null,
    };
  }

  function buildDoubanSearchUrl(item) {
    if (item.doubanId) {
      return (
        'https://movie.douban.com/subject/' +
        encodeURIComponent(
          item.doubanId
        ) +
        '/'
      );
    }

    const preferred =
      item.cnTitle ||
      item.title;

    const query =
      [
        preferred,
        item.year,
      ]
        .filter(Boolean)
        .join(' ');

    return (
      'https://search.douban.com/movie/subject_search?search_text=' +
      encodeURIComponent(query)
    );
  }

  function rowMeta(item) {
    if (state.region === 'china') {
      return (
        item.meta ||
        item.year ||
        ''
      );
    }

    if (state.region === 'europe') {
      return [
        state.type === 'tv'
          ? shortSeason(
              item.season,
              item.title
            )
          : '',
        (item.countryCount || 0) +
          '国入榜',
        (item.totalPoints || 0) +
          '分',
      ]
        .filter(Boolean)
        .join(' · ');
    }

    if (state.region === 'global') {
      return [
        state.type === 'tv'
          ? shortSeason(
              item.season,
              item.title
            )
          : '',
        formatViews(
          item.views
        ),
        item.weeksInTop10
          ? 'TOP10 ' +
            item.weeksInTop10 +
            '周'
          : '',
      ]
        .filter(Boolean)
        .join(' · ');
    }

    return [
      state.type === 'tv'
        ? shortSeason(
            item.season,
            item.title
          )
        : '',
      item.weeksInTop10
        ? 'TOP10 ' +
          item.weeksInTop10 +
          '周'
        : '',
    ]
      .filter(Boolean)
      .join(' · ');
  }

  function itemHref(item) {
    if (state.region === 'china') {
      return item.url || '#';
    }

    return buildDoubanSearchUrl(
      item
    );
  }

  function posterHTML(item) {
    const posters =
      uniqueStrings([
        ...(
          item.posterCandidates ||
          []
        ),
        item.poster,
      ]);

    if (!posters.length) {
      return `
        <div
          class="db-rank-poster-shell"
          data-poster-status="none"
        >
          <div class="db-rank-poster-placeholder">
            影视
          </div>
        </div>
      `;
    }

    return `
      <div
        class="db-rank-poster-shell"
        data-poster-status="pending"
      >
        <div class="db-rank-poster-placeholder">
          影视
        </div>

        <img
          class="db-rank-poster-image"
          data-posters="${esc(
            encodeURIComponent(
              JSON.stringify(
                posters
              )
            )
          )}"
          alt=""
          loading="lazy"
        >
      </div>
    `;
  }

  function itemHTML(item) {
    const meta =
      rowMeta(item);

    const cn =
      state.region !== 'china'
        ? clean(
            item.cnTitle || '',
            220
          )
        : '';

    return `
      <a
        class="db-rank-item${item.rank <= 3 ? ' top-rank' : ''}"
        href="${esc(itemHref(item))}"
        target="_blank"
        rel="noopener noreferrer"
      >
        <div class="db-rank-number">
          ${esc(item.rank)}
        </div>

        ${posterHTML(item)}

        <div class="db-rank-copy">
          <div class="db-rank-title">
            ${esc(item.title)}
          </div>

          ${
            cn
              ? `
                <div class="db-rank-cn">
                  ${esc(cn)}
                </div>
              `
              : ''
          }

          <div class="db-rank-meta">
            ${esc(meta || ' ')}
          </div>
        </div>
      </a>
    `;
  }

  function renderBoard(items) {
    if (
      !Array.isArray(items) ||
      !items.length
    ) {
      return `
        <div class="db-rank-empty">
          暂无榜单数据
        </div>
      `;
    }

    const rows =
      items.slice(0, 10);

    return `
      <div class="db-rank-grid">
        <div>
          ${
            rows
              .slice(0, 5)
              .map(itemHTML)
              .join('')
          }
        </div>

        <div>
          ${
            rows
              .slice(5, 10)
              .map(itemHTML)
              .join('')
          }
        </div>
      </div>
    `;
  }

  function renderDiagnostics() {
    const el =
      document.querySelector(
        '#' +
        ROOT_ID +
        ' .db-rank-diag'
      );

    if (!el) {
      return;
    }

    el.textContent =
      JSON.stringify(
        state.diagnostics,
        null,
        2
      );
  }

  function updatePosterLoadDiagnostics() {
    const root =
      document.getElementById(
        ROOT_ID
      );

    if (!root) {
      return;
    }

    const shells =
      [
        ...root.querySelectorAll(
          '.db-rank-poster-shell'
        ),
      ];

    state.diagnostics.posterLoaded =
      shells.filter(
        shell =>
          shell.dataset
            .posterStatus ===
          'loaded'
      ).length;

    state.diagnostics.posterFailed =
      shells.filter(
        shell =>
          shell.dataset
            .posterStatus ===
          'failed'
      ).length;

    state.diagnostics.posterNoCandidate =
      shells.filter(
        shell =>
          shell.dataset
            .posterStatus ===
          'none'
      ).length;

    renderDiagnostics();
  }

  function installPosterFallbacks() {
    const root =
      document.getElementById(
        ROOT_ID
      );

    if (!root) {
      return;
    }

    root
      .querySelectorAll(
        '.db-rank-poster-image'
      )
      .forEach(img => {
        let posters = [];

        try {
          posters =
            JSON.parse(
              decodeURIComponent(
                img.dataset.posters ||
                '%5B%5D'
              )
            );
        } catch {
          posters = [];
        }

        posters =
          uniqueStrings(posters);

        const shell =
          img.closest(
            '.db-rank-poster-shell'
          );

        let index = 0;

        const apply = () => {
          if (
            index >=
            posters.length
          ) {
            if (shell) {
              shell.dataset
                .posterStatus =
                'failed';
            }

            img.remove();

            updatePosterLoadDiagnostics();
            return;
          }

          const url =
            posters[index++];

          if (
            /images\.justwatch\.com/i
              .test(url)
          ) {
            img.referrerPolicy =
              'no-referrer';
          } else if (
            /doubanio\.com/i
              .test(url)
          ) {
            img.referrerPolicy =
              'origin';
          } else {
            img.referrerPolicy = '';
          }

          img.src = url;
        };

        img.addEventListener(
          'load',
          () => {
            if (shell) {
              shell.dataset
                .posterStatus =
                'loaded';
            }

            updatePosterLoadDiagnostics();
          },
          {
            once: true,
          }
        );

        img.addEventListener(
          'error',
          apply
        );

        apply();
      });

    updatePosterLoadDiagnostics();
  }

  function sourceText() {
    if (state.region === 'china') {
      return state.type === 'movie'
        ? '豆瓣 · 华语电影'
        : '豆瓣 · 国内剧集';
    }

    if (state.region === 'global') {
      return 'Tudum · 全球周榜';
    }

    if (state.region === 'europe') {
      return 'Tudum · 欧洲20国综合';
    }

    return (
      'Tudum · ' +
      REGION_LABELS[state.region] +
      '周榜'
    );
  }

  function renderTabs() {
    document
      .querySelectorAll(
        '#' +
        ROOT_ID +
        ' [data-region]'
      )
      .forEach(button => {
        button.classList.toggle(
          'active',
          button.dataset.region ===
            state.region
        );
      });

    document
      .querySelectorAll(
        '#' +
        ROOT_ID +
        ' [data-type]'
      )
      .forEach(button => {
        button.classList.toggle(
          'active',
          button.dataset.type ===
            state.type
        );
      });
  }

  function setStatus(
    text,
    kind = ''
  ) {
    const el =
      document.querySelector(
        '#' +
        ROOT_ID +
        ' .db-rank-status'
      );

    if (!el) {
      return;
    }

    el.className =
      'db-rank-status ' + kind;

    el.textContent = text;
  }

  function renderSyncNote(
    consistency
  ) {
    const el =
      document.querySelector(
        '#' +
        ROOT_ID +
        ' .db-rank-sync-note'
      );

    if (!el) {
      return;
    }

    const updating =
      Boolean(
        consistency?.updating
      );

    el.classList.toggle(
      'show',
      updating
    );

    el.textContent =
      updating
        ? '数据更新中'
        : '';

    el.title =
      updating
        ? (
            consistency
              ?.syncReasons
              ?.join('；') ||
            '数据周暂未完全同步'
          )
        : '';
  }

  function renderFooter(
    payload,
    consistency =
      state.weekConsistency
  ) {
    const source =
      document.querySelector(
        '#' +
        ROOT_ID +
        ' .db-rank-source'
      );

    const week =
      document.querySelector(
        '#' +
        ROOT_ID +
        ' .db-rank-week'
      );

    if (source) {
      source.textContent =
        sourceText();
    }

    if (week) {
      week.textContent =
        payload?.week
          ? '数据周：' +
            payload.week
          : '最近热门';
    }

    renderSyncNote(
      consistency
    );
  }

  async function loadCurrent(
    force = false
  ) {
    const serial =
      ++state.requestSerial;

    state.weekConsistency =
      null;

    renderTabs();

    setStatus(
      force
        ? '正在刷新…'
        : '正在更新…'
    );

    const board =
      document.querySelector(
        '#' +
        ROOT_ID +
        ' .db-rank-board'
      );

    if (!board) {
      return;
    }

    board.innerHTML = `
      <div class="db-rank-loading">
        加载中…
      </div>
    `;

    try {
      const result =
        await fetchDataset(force);

      if (
        serial !==
        state.requestSerial
      ) {
        return;
      }

      const payload =
        result.payload;

      const items =
        payload.items || [];

      state.current =
        payload;

      state.diagnostics = {
        version: VERSION,
        region: state.region,
        type: state.type,
        source:
          payload.source,
        methodology:
          payload.methodology,

        dataHost:
          payload.dataHost ||
          (
            state.region ===
            'china'
              ? 'Douban'
              : 'KimsaiKwa/Netflix_Global'
          ),

        metadataMode:
          payload.metadataMode ||
          null,

        metadataVersion:
          payload.metadataVersion ??
          null,

        metadataEnrichedAt:
          payload.metadataEnrichedAt ||
          null,

        week:
          payload.week || null,

        count:
          items.length,

        cacheState:
          result.cacheState,

        cachedAt:
          result.cachedAt
            ? new Date(
                result.cachedAt
              ).toISOString()
            : null,

        fetchWarning:
          result.error || null,

        validCountryCount:
          payload.validCountryCount ||
          null,

        totalCountryResponses:
          payload.totalCountryResponses ||
          null,

        allCountriesAvailable:
          payload.allCountriesAvailable ??
          null,

        countryWeeksConsistent:
          payload.countryWeeksConsistent ??
          null,

        weekDistribution:
          payload.weekDistribution ||
          null,

        outOfWeekCountries:
          payload.outOfWeekCountries ||
          [],

        failedCountries:
          payload.failedCountries ||
          [],

        posterResolved:
          items.filter(
            item =>
              item.posterCandidates
                ?.length
          ).length,

        cnTitleResolved:
          items.filter(
            item =>
              Boolean(
                item.cnTitle
              )
          ).length,

        doubanIdResolved:
          items.filter(
            item =>
              Boolean(
                item.doubanId
              )
          ).length,

        tmdbIdResolved:
          items.filter(
            item =>
              Boolean(
                item.tmdbId
              )
          ).length,
      };

      board.innerHTML =
        renderBoard(items);

      installPosterFallbacks();

      renderFooter(
        payload,
        null
      );

      renderDiagnostics();

      if (
        result.cacheState ===
        'stale'
      ) {
        setStatus(
          '更新失败，正在显示旧缓存：' +
          (
            result.error ||
            '未知错误'
          ),
          'warn'
        );
      } else {
        setStatus('');
      }

      if (state.region !== 'china') {
        assessWeekConsistency(
          payload,
          force
        )
          .then(
            consistency => {
              if (
                serial !==
                state.requestSerial
              ) {
                return;
              }

              state.weekConsistency =
                consistency;

              state.diagnostics.dataWeek =
                consistency.dataWeek;

              state.diagnostics.referenceWeek =
                consistency.referenceWeek;

              state.diagnostics.weekConsistent =
                consistency.weekConsistent;

              state.diagnostics.syncStatus =
                consistency.syncStatus;

              state.diagnostics.syncReasons =
                consistency.syncReasons;

              state.diagnostics.referenceWeekError =
                consistency.referenceError ||
                null;

              state.diagnostics.referenceCacheState =
                consistency.referenceCacheState ||
                null;

              state.diagnostics.referenceCachedAt =
                consistency.referenceCachedAt ||
                null;

              renderFooter(
                payload,
                consistency
              );

              renderDiagnostics();
            }
          )
          .catch(() => {});
      }
    } catch (error) {
      if (
        serial !==
        state.requestSerial
      ) {
        return;
      }

      state.current = null;
      state.weekConsistency = null;

      state.diagnostics = {
        version: VERSION,
        region: state.region,
        type: state.type,
        error:
          error.message ||
          String(error),
      };

      board.innerHTML = `
        <div class="db-rank-empty">
          更新失败：${esc(
            error.message ||
            error
          )}
        </div>
      `;

      renderFooter(
        null,
        null
      );

      renderDiagnostics();

      setStatus(
        '更新失败：' +
        (
          error.message ||
          error
        ),
        'error'
      );
    }
  }

  function cleanupLegacy() {
    [
      'db-stream-rank-v150',
      'db-stream-rank-v140',
      'db-stream-rank-v131',
      'db-stream-rank-v130',
      'db-stream-rank-v120',
      'db-stream-rank-v110',
      'db-stream-rank-v100',
      'db-global-rank-v091',
      'db-global-rank-v090',
      'db-global-rank-v041',
      'db-global-rank-v04',
      'db-global-rank',
    ].forEach(id => {
      document
        .getElementById(id)
        ?.remove();
    });

    [
      'db-stream-rank-v150-style',
      'db-stream-rank-v140-style',
      'db-stream-rank-v131-style',
      'db-stream-rank-v130-style',
      'db-stream-rank-v120-style',
      'db-stream-rank-v110-style',
      'db-stream-rank-v100-style',
      'db-global-rank-v091-style',
      'db-global-rank-v090-style',
      'db-global-rank-v041-style',
      'db-global-rank-v04-style',
      'db-global-rank-style',
    ].forEach(id => {
      document
        .getElementById(id)
        ?.remove();
    });
  }

  function injectStyles() {
    document
      .getElementById(
        STYLE_ID
      )
      ?.remove();

    const style =
      document.createElement(
        'style'
      );

    style.id = STYLE_ID;

    style.textContent = `
      @media (min-width: 1420px) {
        #wrapper {
          width: 1365px !important;
        }

        #content {
          width: 1365px !important;
        }

        #content .grid-16-8 {
          width: 1365px !important;
          display: grid !important;
          grid-template-columns: 715px 620px !important;
          column-gap: 30px !important;
          align-items: start !important;
          overflow: visible !important;
        }

        #content .grid-16-8 > .article {
          width: 675px !important;
          float: none !important;
          margin: 0 !important;
          min-width: 0 !important;
        }

        #content .grid-16-8 > .aside {
          width: 620px !important;
          float: none !important;
          margin: 0 !important;
          min-width: 0 !important;
        }
      }

      @media (max-width: 1419px) {
        #wrapper,
        #content {
          width: min(
            715px,
            calc(100% - 30px)
          ) !important;
        }

        #content .grid-16-8 {
          width: 100% !important;
          display: flex !important;
          flex-direction: column !important;
          align-items: center !important;
          gap: 24px !important;
          overflow: visible !important;
        }

        #content .grid-16-8 > .article {
          order: 2;
          width: min(
            675px,
            100%
          ) !important;
          float: none !important;
          margin: 0 !important;
        }

        #content .grid-16-8 > .aside {
          order: 1;
          width: min(
            620px,
            100%
          ) !important;
          float: none !important;
          margin: 0 !important;
        }
      }

      #${ROOT_ID} {
        width: 620px;
        max-width: 100%;
        box-sizing: border-box;
        background: #fff;
        border: 1px solid #e6ebf1;
        border-radius: 16px;
        box-shadow:
          0 10px 32px
          rgba(30, 55, 85, .08);
        padding:
          16px 16px 12px;
        color: #29384b;

        font-family:
          -apple-system,
          BlinkMacSystemFont,
          "PingFang SC",
          "Microsoft YaHei",
          sans-serif;
      }

      #${ROOT_ID},
      #${ROOT_ID} * {
        box-sizing: border-box;
      }

      #${ROOT_ID} .db-rank-head {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 14px;
      }

      #${ROOT_ID} .db-rank-heading {
        font-size: 18px;
        line-height: 30px;
        font-weight: 700;
        color: #203047;
      }

      #${ROOT_ID} .db-rank-refresh {
        margin-left: auto;
        border: 1px solid #dfe6ef;
        border-radius: 8px;
        background: #fff;
        color: #60758f;
        height: 30px;
        padding: 0 11px;
        cursor: pointer;
        font-size: 11px;
      }

      #${ROOT_ID} .db-rank-refresh:hover {
        background: #f7f9fc;
      }

      #${ROOT_ID} .db-rank-regions {
        display: grid;
        grid-template-columns:
          repeat(
            6,
            minmax(0, 1fr)
          );
        gap: 6px;
        margin-bottom: 8px;
      }

      #${ROOT_ID} .db-rank-types {
        display: grid;
        grid-template-columns:
          repeat(
            2,
            minmax(0, 1fr)
          );
        gap: 7px;
        margin-bottom: 7px;
      }

      #${ROOT_ID} .db-rank-region,
      #${ROOT_ID} .db-rank-type {
        min-width: 0;
        border: 1px solid #dfe6ef;
        border-radius: 8px;
        background: #fff;
        color: #60758f;
        cursor: pointer;
        text-align: center;
        white-space: nowrap;
        transition: all .15s ease;
      }

      #${ROOT_ID} .db-rank-region {
        padding: 6px 4px;
        font-size: 10.5px;
      }

      #${ROOT_ID} .db-rank-type {
        padding: 7px 8px;
        font-size: 11px;
      }

      #${ROOT_ID} .db-rank-region:hover,
      #${ROOT_ID} .db-rank-type:hover {
        background: #f8faff;
      }

      #${ROOT_ID} .db-rank-region.active,
      #${ROOT_ID} .db-rank-type.active {
        border-color: #4d82e2;
        background: #edf4ff;
        color: #2b64c4;
        font-weight: 600;
      }

      #${ROOT_ID} .db-rank-status {
        min-height: 17px;
        padding: 0 1px 5px;
        color: #8794a6;
        font-size: 10px;
      }

      #${ROOT_ID} .db-rank-status:empty {
        display: none;
      }

      #${ROOT_ID} .db-rank-status.warn {
        color: #9b6a2f;
      }

      #${ROOT_ID} .db-rank-status.error {
        color: #b34d4d;
      }

      #${ROOT_ID} .db-rank-board {
        min-height: 250px;
      }

      #${ROOT_ID} .db-rank-grid {
        display: grid;
        grid-template-columns:
          minmax(0, 1fr)
          minmax(0, 1fr);
        gap: 0 14px;
      }

      #${ROOT_ID} .db-rank-item {
        display: grid;
        grid-template-columns:
          24px
          44px
          minmax(0, 1fr);
        gap: 8px;
        align-items: center;
        min-height: 80px;
        padding: 6px 3px 6px 0;
        border-bottom: 1px solid #f0f3f7;
        color: inherit;
        text-decoration: none !important;
        border-radius: 5px;
      }

      #${ROOT_ID} .db-rank-item:hover {
        background: #fafcff;
      }

      #${ROOT_ID} .db-rank-number {
        text-align: center;
        font-size: 17px;
        font-weight: 700;
        color: #9aa7b7;
      }

      #${ROOT_ID} .db-rank-item.top-rank .db-rank-number {
        color: #4779d5;
      }

      #${ROOT_ID} .db-rank-poster-shell {
        position: relative;
        width: 44px;
        height: 66px;
        overflow: hidden;
        border-radius: 5px;
        background: #eef1f5;
        box-shadow:
          0 1px 4px
          rgba(24, 40, 60, .08);
      }

      #${ROOT_ID} .db-rank-poster-placeholder,
      #${ROOT_ID} .db-rank-poster-image {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
      }

      #${ROOT_ID} .db-rank-poster-placeholder {
        display: flex;
        align-items: center;
        justify-content: center;
        color: #a6b0bd;
        font-size: 10px;
        background:
          linear-gradient(
            145deg,
            #f5f7fa,
            #e9edf2
          );
      }

      #${ROOT_ID} .db-rank-poster-image {
        display: block;
        object-fit: cover;
        background: #eef1f5;
      }

      #${ROOT_ID} .db-rank-copy {
        min-width: 0;
      }

      #${ROOT_ID} .db-rank-title {
        color: #2e3d51;
        font-size: 13px;
        line-height: 1.27;
        font-weight: 650;
        display: -webkit-box;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
        overflow: hidden;
      }

      #${ROOT_ID} .db-rank-cn {
        margin-top: 3px;
        color: #718095;
        font-size: 10.5px;
        line-height: 1.25;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      #${ROOT_ID} .db-rank-meta {
        margin-top: 4px;
        color: #98a4b3;
        font-size: 9.5px;
        line-height: 1.25;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      #${ROOT_ID} .db-rank-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        padding-top: 10px;
        color: #9aa6b5;
        font-size: 9px;
      }

      #${ROOT_ID} .db-rank-source {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      #${ROOT_ID} .db-rank-footer-right {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 6px;
        flex-shrink: 0;
      }

      #${ROOT_ID} .db-rank-sync-note {
        display: none;
        align-items: center;
        height: 18px;
        padding: 0 6px;
        border: 1px solid #ead8b8;
        border-radius: 999px;
        background: #fff9ef;
        color: #a87937;
        font-size: 9px;
        white-space: nowrap;
      }

      #${ROOT_ID} .db-rank-sync-note.show {
        display: inline-flex;
      }

      #${ROOT_ID} details {
        margin-top: 5px;
        color: #b1bac6;
        font-size: 9px;
      }

      #${ROOT_ID} details summary {
        display: inline-block;
        cursor: pointer;
        list-style: none;
        user-select: none;
        color: #aab4c0;
        transition: color .15s ease;
      }

      #${ROOT_ID} details summary::-webkit-details-marker {
        display: none;
      }

      #${ROOT_ID} details summary::after {
        content: " ›";
      }

      #${ROOT_ID} details[open] summary::after {
        content: " ⌄";
      }

      #${ROOT_ID} details summary:hover {
        color: #758499;
      }

      /* v1.6: remove the browser's default blue focus rectangle. */
      #${ROOT_ID} details summary:focus,
      #${ROOT_ID} details summary:focus-visible {
        outline: none !important;
        box-shadow: none !important;
      }

      #${ROOT_ID} .db-rank-diag {
        margin: 7px 0 0;
        padding: 8px;
        border-radius: 7px;
        background: #f7f9fb;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        color: #768599;
        font:
          9px/1.45
          ui-monospace,
          SFMono-Regular,
          Menlo,
          monospace;
      }

      #${ROOT_ID} .db-rank-empty,
      #${ROOT_ID} .db-rank-loading {
        padding: 45px 15px;
        text-align: center;
        color: #909dac;
        font-size: 11px;
      }

      @media (max-width: 680px) {
        #${ROOT_ID} .db-rank-regions {
          grid-template-columns:
            repeat(3, 1fr);
        }

        #${ROOT_ID} .db-rank-grid {
          grid-template-columns: 1fr;
        }
      }
    `;

    document.head.appendChild(style);
  }

  function ensureAside() {
    const grid =
      document.querySelector(
        '#content .grid-16-8'
      );

    if (!grid) {
      return null;
    }

    let aside =
      grid.querySelector(
        ':scope > .aside'
      );

    if (!aside) {
      aside =
        document.createElement(
          'div'
        );

      aside.className = 'aside';

      grid.appendChild(aside);
    }

    return aside;
  }

  function createPanel() {
    document
      .getElementById(
        ROOT_ID
      )
      ?.remove();

    const aside =
      ensureAside();

    if (!aside) {
      return null;
    }

    const panel =
      document.createElement(
        'section'
      );

    panel.id = ROOT_ID;

    panel.innerHTML = `
      <div class="db-rank-head">
        <div class="db-rank-heading">
          影视热榜
        </div>

        <button
          class="db-rank-refresh"
          type="button"
        >
          刷新
        </button>
      </div>

      <div class="db-rank-regions">
        ${
          Object
            .entries(
              REGION_LABELS
            )
            .map(
              ([key, label]) => `
                <button
                  class="db-rank-region"
                  type="button"
                  data-region="${key}"
                >
                  ${label}
                </button>
              `
            )
            .join('')
        }
      </div>

      <div class="db-rank-types">
        <button
          class="db-rank-type"
          type="button"
          data-type="movie"
        >
          电影 TOP 10
        </button>

        <button
          class="db-rank-type"
          type="button"
          data-type="tv"
        >
          电视剧 TOP 10
        </button>
      </div>

      <div class="db-rank-status"></div>

      <div class="db-rank-board">
        <div class="db-rank-loading">
          加载中…
        </div>
      </div>

      <div class="db-rank-footer">
        <div class="db-rank-source"></div>

        <div class="db-rank-footer-right">
          <span class="db-rank-sync-note"></span>
          <span class="db-rank-week"></span>
        </div>
      </div>

      <details>
        <summary>详情</summary>
        <pre class="db-rank-diag"></pre>
      </details>
    `;

    aside.insertBefore(
      panel,
      aside.firstChild
    );

    panel
      .querySelectorAll(
        '[data-region]'
      )
      .forEach(button => {
        button.addEventListener(
          'click',
          () => {
            const next =
              button.dataset.region;

            if (
              next === state.region
            ) {
              return;
            }

            state.region = next;

            savePref(
              'region',
              next
            );

            loadCurrent(false);
          }
        );
      });

    panel
      .querySelectorAll(
        '[data-type]'
      )
      .forEach(button => {
        button.addEventListener(
          'click',
          () => {
            const next =
              button.dataset.type;

            if (
              next === state.type
            ) {
              return;
            }

            state.type = next;

            savePref(
              'type',
              next
            );

            loadCurrent(false);
          }
        );
      });

    panel
      .querySelector(
        '.db-rank-refresh'
      )
      .addEventListener(
        'click',
        () => {
          const now =
            Date.now();

          if (
            now -
              state.lastManualRefreshAt <
            REFRESH_THROTTLE
          ) {
            const left =
              Math.ceil(
                (
                  REFRESH_THROTTLE -
                  (
                    now -
                    state.lastManualRefreshAt
                  )
                ) /
                1000
              );

            setStatus(
              '刷新过于频繁，请 ' +
              left +
              ' 秒后再试',
              'warn'
            );

            return;
          }

          state.lastManualRefreshAt =
            now;

          loadCurrent(true);
        }
      );

    return panel;
  }

  function alignPanel() {
    const panel =
      document.getElementById(
        ROOT_ID
      );

    const title =
      document.querySelector(
        '#content > h1, #content h1'
      );

    if (
      !panel ||
      !title
    ) {
      return;
    }

    panel.style.position = '';
    panel.style.top = '';

    if (
      window.innerWidth < 1420
    ) {
      return;
    }

    requestAnimationFrame(() => {
      const panelTop =
        panel
          .getBoundingClientRect()
          .top;

      const titleTop =
        title
          .getBoundingClientRect()
          .top;

      const delta =
        Math.max(
          0,
          Math.min(
            160,
            panelTop - titleTop
          )
        );

      if (delta > 0) {
        panel.style.position =
          'relative';

        panel.style.top =
          '-' +
          Math.round(delta) +
          'px';
      }
    });
  }

  function init() {
    cleanupLegacy();
    injectStyles();

    const panel =
      createPanel();

    if (!panel) {
      return;
    }

    renderTabs();

    requestAnimationFrame(() => {
      alignPanel();

      setTimeout(
        alignPanel,
        250
      );

      setTimeout(
        alignPanel,
        800
      );
    });

    window.addEventListener(
      'resize',
      () => {
        clearTimeout(
          init.resizeTimer
        );

        init.resizeTimer =
          setTimeout(
            alignPanel,
            120
          );
      }
    );

    loadCurrent(false);
  }

  init();
})();
