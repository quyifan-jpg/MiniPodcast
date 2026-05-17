import axios from 'axios';

// 生产部署时设置 REACT_APP_API_URL 为后端地址（如 ECS 负载均衡器或 API 网关）
const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:18000';

const api = axios.create({
   baseURL: API_BASE_URL,
   timeout: 60000 * 5,
   headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
   },
});

export const TOKEN_STORAGE_KEY = 'miniblog_access_token';
export const REFRESH_TOKEN_STORAGE_KEY = 'miniblog_refresh_token';

const isDev = process.env.NODE_ENV !== 'production';

// Hooks injected by AuthProvider so the interceptor can navigate / surface
// errors through React Router instead of doing a full-page reload.
let onUnauthorized = null;
let onRateLimited = null;

export const setAuthHandlers = ({ onUnauthorized: u, onRateLimited: r }) => {
   onUnauthorized = u || null;
   onRateLimited = r || null;
};

// Single in-flight refresh promise — concurrent 401s share one /refresh call.
let refreshInFlight = null;

const refreshAccessToken = async () => {
   const refreshToken = localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY);
   if (!refreshToken) {
      throw new Error('No refresh token available');
   }
   if (!refreshInFlight) {
      // Use a bare axios instance so the request bypasses our interceptors —
      // avoids recursion if the refresh itself 401s.
      refreshInFlight = axios
         .post(
            `${API_BASE_URL}/api/auth/refresh`,
            { refresh_token: refreshToken },
            { headers: { 'Content-Type': 'application/json' } }
         )
         .then(res => {
            const newAccess = res.data.access_token;
            localStorage.setItem(TOKEN_STORAGE_KEY, newAccess);
            return newAccess;
         })
         .finally(() => {
            refreshInFlight = null;
         });
   }
   return refreshInFlight;
};

api.interceptors.request.use(
   config => {
      const token = localStorage.getItem(TOKEN_STORAGE_KEY);
      if (token) {
         config.headers = config.headers || {};
         config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
   },
   error => {
      return Promise.reject(error);
   }
);

api.interceptors.response.use(
   response => {
      if (
         response.data &&
         response.data.items &&
         Array.isArray(response.data.items) &&
         response.config.url.includes('/api/articles')
      ) {
         response.data.items = response.data.items.map(normalizeArticleData);
      } else if (
         response.data &&
         response.config.url.includes('/api/articles/') &&
         !response.config.url.includes('/list')
      ) {
         response.data = normalizeArticleData(response.data);
      }
      return response;
   },
   async error => {
      if (error.response) {
         if (isDev) {
            console.error('API Error:', error.response.status, error.response.data);
         }
         const url = error.config?.url || '';
         const isAuthRoute =
            url.includes('/api/auth/login') ||
            url.includes('/api/auth/register') ||
            url.includes('/api/auth/refresh');

         if (error.response.status === 401 && !isAuthRoute) {
            const originalRequest = error.config;
            // Try one auto-refresh before giving up.
            if (
               !originalRequest._retriedAfterRefresh &&
               localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY)
            ) {
               originalRequest._retriedAfterRefresh = true;
               try {
                  const newAccess = await refreshAccessToken();
                  originalRequest.headers = originalRequest.headers || {};
                  originalRequest.headers.Authorization = `Bearer ${newAccess}`;
                  return api(originalRequest);
               } catch (refreshErr) {
                  if (isDev) console.warn('Token refresh failed:', refreshErr?.message);
                  // fall through to logout
               }
            }
            localStorage.removeItem(TOKEN_STORAGE_KEY);
            localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY);
            if (onUnauthorized) {
               onUnauthorized();
            }
         } else if (error.response.status === 429 && onRateLimited) {
            onRateLimited(error.response);
         }
      } else if (error.request) {
         if (isDev) console.error('No response received:', error.request);
      } else if (isDev) {
         console.error('Error setting up request:', error.message);
      }
      return Promise.reject(error);
   }
);

const normalizeArticleData = article => {
   if (!article) return article;
   if (!article.categories) {
      article.categories = [];
   } else if (!Array.isArray(article.categories)) {
      if (typeof article.categories === 'string') {
         article.categories = article.categories.split(',').map(c => c.trim());
      } else {
         article.categories = [article.categories];
      }
   }
   return article;
};

const endpoints = {
   root: {
      get: () => api.get('/api'),
   },
   auth: {
      register: ({ email, username, password }) =>
         api.post('/api/auth/register', { email, username, password }),
      login: ({ email, password }) =>
         api.post('/api/auth/login', { email, password }),
      refresh: refreshToken =>
         api.post('/api/auth/refresh', { refresh_token: refreshToken }),
      me: () => api.get('/api/auth/me'),
      updateProfile: ({ username, email }) =>
         api.patch('/api/auth/me', { username, email }),
      changePassword: ({ currentPassword, newPassword }) =>
         api.post('/api/auth/change-password', {
            current_password: currentPassword,
            new_password: newPassword,
         }),
      deactivateAccount: () => api.delete('/api/auth/me'),
   },
   articles: {
      getAll: (params = {}) => api.get('/api/articles/', { params }),
      getById: articleId => api.get(`/api/articles/${articleId}`),
      getSources: () => api.get('/api/articles/sources/list'),
      getCategories: () => api.get('/api/articles/categories/list'),
   },
   podcasts: {
      getAll: (params = {}) => api.get('/api/podcasts/', { params }),
      getById: podcastId => api.get(`/api/podcasts/${podcastId}`),
      getByIdentifier: identifier => api.get(`/api/podcasts/by-identifier/${identifier}`),
      getFormats: () => api.get('/api/podcasts/formats'),
      getLanguageCodes: () => api.get('/api/podcasts/language-codes'),
      getTtsEngines: () => api.get('/api/podcasts/tts-engines'),
      getAudioUrl: filename => `${API_BASE_URL}/audio/${filename}`,
      create: podcastData => api.post('/api/podcasts/', podcastData),
      update: (podcastId, podcastData) => api.put(`/api/podcasts/${podcastId}`, podcastData),
      delete: podcastId => api.delete(`/api/podcasts/${podcastId}`),
      uploadAudio: (podcastId, audioFile) => {
         const formData = new FormData();
         formData.append('file', audioFile);
         return api.post(`/api/podcasts/${podcastId}/audio`, formData, {
            headers: { 'Content-Type': 'multipart/form-data' },
         });
      },
      uploadBanner: (podcastId, imageFile) => {
         const formData = new FormData();
         formData.append('file', imageFile);
         return api.post(`/api/podcasts/${podcastId}/banner`, formData, {
            headers: { 'Content-Type': 'multipart/form-data' },
         });
      },
   },
   sources: {
      getAll: (params = {}) => api.get('/api/sources/', { params }),
      getById: sourceId => api.get(`/api/sources/${sourceId}`),
      create: sourceData => api.post('/api/sources/', sourceData),
      update: (sourceId, sourceData) => api.put(`/api/sources/${sourceId}`, sourceData),
      delete: (sourceId, permanent = true) =>
         api.delete(`/api/sources/${sourceId}`, { params: { permanent } }),
      getByName: name => api.get(`/api/sources/by-name/${name}`),
      getByCategory: category => api.get(`/api/sources/by-category/${category}`),
      getCategories: () => api.get('/api/sources/categories'),
      getFeeds: sourceId => api.get(`/api/sources/${sourceId}/feeds`),
      addFeed: (sourceId, feedData) => api.post(`/api/sources/${sourceId}/feeds`, feedData),
      updateFeed: (feedId, feedData) => api.put(`/api/sources/feeds/${feedId}`, feedData),
      deleteFeed: feedId => api.delete(`/api/sources/feeds/${feedId}`),
   },
   tasks: {
      getAll: (includeDisabled = false) =>
         api.get('/api/tasks/', { params: { include_disabled: includeDisabled } }),
      getById: taskId => api.get(`/api/tasks/${taskId}`),
      create: taskData => api.post('/api/tasks/', taskData),
      update: (taskId, taskData) => api.put(`/api/tasks/${taskId}`, taskData),
      delete: taskId => api.delete(`/api/tasks/${taskId}`),
      getPending: () => api.get('/api/tasks/pending'),
      getExecutions: (options = {}) => {
         const { taskId = null, page = 1, perPage = 10 } = options;
         const params = {
            page,
            per_page: perPage,
         };
         if (taskId) params.task_id = taskId;
         return api.get('/api/tasks/executions', { params });
      },
      getStats: () => api.get('/api/tasks/stats'),
      getTypes: () => api.get('/api/tasks/types'),
      enable: taskId => api.post(`/api/tasks/${taskId}/enable`),
      disable: taskId => api.post(`/api/tasks/${taskId}/disable`),
   },
   podcastConfigs: {
      getAll: (activeOnly = false) =>
         api.get('/api/podcast-configs/', { params: { active_only: activeOnly } }),
      getById: configId => api.get(`/api/podcast-configs/${configId}`),
      create: configData => api.post('/api/podcast-configs/', configData),
      update: (configId, configData) => api.put(`/api/podcast-configs/${configId}`, configData),
      delete: configId => api.delete(`/api/podcast-configs/${configId}`),
      toggle: (configId, isActive) =>
         api.post(`/api/podcast-configs/${configId}/${isActive ? 'enable' : 'disable'}`),
      getTtsEngines: () => api.get('/api/podcasts/tts-engines'),
      getLanguageCodes: () => api.get('/api/podcasts/language-codes'),
   },
   podcastAgent: {
      languages: () => api.get('/api/podcast-agent/languages'),
      createSession: (sessionId = null) =>
         api.post('/api/podcast-agent/session', {
            session_id: sessionId,
         }),
      chat: (sessionId, message) =>
         api.post('/api/podcast-agent/chat', {
            session_id: sessionId,
            message,
         }),
      checkStatus: (sessionId, taskId = null) =>
         api.post('/api/podcast-agent/status', {
            session_id: sessionId,
            task_id: taskId,
         }),
      getLatestMessage: sessionId =>
         api.get(`/api/podcast-agent/latest_message?session_id=${sessionId}`),
      listSessions: (page = 1, perPage = 10) =>
         api.get('/api/podcast-agent/sessions', {
            params: { page, per_page: perPage },
         }),
      deleteSession: sessionId => api.delete(`/api/podcast-agent/session/${sessionId}`),
      getSessionHistory: sessionId =>
         api.get(`/api/podcast-agent/session_history?session_id=${sessionId}`),
      getBannerUrl: filename => `${API_BASE_URL}/podcast_img/${filename}`,
      getAudioUrl: filename => `${API_BASE_URL}/audio/${filename}`,
   },

   socialMedia: {
      getAll: (params = {}) => api.get('/api/social-media/', { params }),
      getById: postId => api.get(`/api/social-media/${postId}`),
      getPlatforms: () => api.get('/api/social-media/platforms/list'),
      getSentiments: (dateFrom, dateTo) =>
         api.get('/api/social-media/sentiments/list', {
            params: {
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getTopUsers: (limit = 10, platform = null, dateFrom = null, dateTo = null) =>
         api.get('/api/social-media/users/top', {
            params: {
               limit,
               platform,
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getCategories: (dateFrom = null, dateTo = null) =>
         api.get('/api/social-media/categories/list', {
            params: {
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getUserSentiment: (limit = 10, platform = null, dateFrom = null, dateTo = null) =>
         api.get('/api/social-media/users/sentiment', {
            params: {
               limit,
               platform,
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getCategorySentiment: (dateFrom = null, dateTo = null) =>
         api.get('/api/social-media/categories/sentiment', {
            params: {
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getTrendingTopics: (dateFrom = null, dateTo = null, limit = 10) =>
         api.get('/api/social-media/topic/trends', {
            params: {
               limit,
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getSentimentOverTime: (dateFrom = null, dateTo = null, platform = null) =>
         api.get('/api/social-media/trends/time', {
            params: {
               platform,
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getInfluentialPosts: (sentiment = null, limit = 5, dateFrom = null, dateTo = null) =>
         api.get('/api/social-media/posts/influential', {
            params: {
               sentiment,
               limit,
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),
      getEngagementStats: (dateFrom = null, dateTo = null) =>
         api.get('/api/social-media/engagement/stats', {
            params: {
               date_from: dateFrom,
               date_to: dateTo,
            },
         }),

      setupSession: (sites = null) => {
         return api.post('/api/social-media/session/setup', null, {});
      },
   },

   API_BASE_URL: API_BASE_URL,
};

export default endpoints;
