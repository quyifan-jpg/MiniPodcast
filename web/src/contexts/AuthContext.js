import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import api, {
   TOKEN_STORAGE_KEY,
   REFRESH_TOKEN_STORAGE_KEY,
   setAuthHandlers,
} from '../services/api';

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
   const [user, setUser] = useState(null);
   const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY));
   const [loading, setLoading] = useState(Boolean(localStorage.getItem(TOKEN_STORAGE_KEY)));
   const [rateLimitMessage, setRateLimitMessage] = useState(null);
   const navigate = useNavigate();
   const location = useLocation();

   const persistTokens = useCallback((accessToken, refreshToken) => {
      if (accessToken) {
         localStorage.setItem(TOKEN_STORAGE_KEY, accessToken);
      } else {
         localStorage.removeItem(TOKEN_STORAGE_KEY);
      }
      if (refreshToken !== undefined) {
         if (refreshToken) {
            localStorage.setItem(REFRESH_TOKEN_STORAGE_KEY, refreshToken);
         } else {
            localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY);
         }
      }
      setToken(accessToken);
   }, []);

   useEffect(() => {
      setAuthHandlers({
         onUnauthorized: () => {
            persistTokens(null, null);
            setUser(null);
            const path = location.pathname + location.search;
            if (location.pathname !== '/login' && location.pathname !== '/register') {
               navigate(`/login?redirect=${encodeURIComponent(path)}`, { replace: true });
            }
         },
         onRateLimited: response => {
            const retryAfter = response?.headers?.['retry-after'];
            setRateLimitMessage(
               retryAfter
                  ? `Too many requests. Try again in ${retryAfter}s.`
                  : 'Too many requests. Please slow down and try again shortly.'
            );
            setTimeout(() => setRateLimitMessage(null), 5000);
         },
      });
      return () => setAuthHandlers({ onUnauthorized: null, onRateLimited: null });
   }, [navigate, location.pathname, location.search, persistTokens]);

   const fetchMe = useCallback(async () => {
      try {
         const res = await api.auth.me();
         setUser(res.data);
         return res.data;
      } catch (err) {
         setUser(null);
         // Don't drop the refresh token here — the interceptor already
         // attempted refresh; if /me still 401s, the unauthorized handler
         // clears both tokens.
         throw err;
      }
   }, []);

   useEffect(() => {
      if (!token) {
         setLoading(false);
         return;
      }
      let cancelled = false;
      (async () => {
         try {
            await fetchMe();
         } catch {
            // interceptor will redirect on 401; nothing else to do
         } finally {
            if (!cancelled) setLoading(false);
         }
      })();
      return () => {
         cancelled = true;
      };
   }, [token, fetchMe]);

   const login = async ({ email, password }) => {
      const res = await api.auth.login({ email, password });
      persistTokens(res.data.access_token, res.data.refresh_token);
      const me = await api.auth.me();
      setUser(me.data);
      return me.data;
   };

   const register = async ({ email, username, password }) => {
      const res = await api.auth.register({ email, username, password });
      persistTokens(res.data.access_token, res.data.refresh_token);
      const me = await api.auth.me();
      setUser(me.data);
      return me.data;
   };

   const logout = () => {
      persistTokens(null, null);
      setUser(null);
   };

   const refreshUser = useCallback(async () => {
      const me = await api.auth.me();
      setUser(me.data);
      return me.data;
   }, []);

   const value = {
      user,
      token,
      isAuthenticated: Boolean(token && user),
      loading,
      login,
      register,
      logout,
      refreshUser,
      rateLimitMessage,
   };

   return (
      <AuthContext.Provider value={value}>
         {children}
         {rateLimitMessage && (
            <div
               role="alert"
               className="fixed bottom-4 right-4 z-50 px-4 py-3 bg-amber-900/90 border border-amber-700 text-amber-100 rounded shadow-lg max-w-sm text-sm"
            >
               {rateLimitMessage}
            </div>
         )}
      </AuthContext.Provider>
   );
};

export const useAuth = () => {
   const ctx = useContext(AuthContext);
   if (!ctx) {
      throw new Error('useAuth must be used inside <AuthProvider>');
   }
   return ctx;
};
