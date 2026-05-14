import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import api from '../services/api';

const AuthContext = createContext(null);

const TOKEN_KEY = 'mp_access_token';

export function AuthProvider({ children }) {
   const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY));
   const [user, setUser]   = useState(null);
   const [loading, setLoading] = useState(!!localStorage.getItem(TOKEN_KEY));

   // Fetch /me whenever we have a token
   useEffect(() => {
      if (!token) {
         setUser(null);
         setLoading(false);
         return;
      }
      setLoading(true);
      api.auth.me()
         .then(res => setUser(res.data))
         .catch(() => {
            // Token invalid / expired — clear it
            localStorage.removeItem(TOKEN_KEY);
            setToken(null);
            setUser(null);
         })
         .finally(() => setLoading(false));
   }, [token]);

   const login = useCallback(async (email, password) => {
      const res = await api.auth.login({ email, password });
      const { access_token } = res.data;
      localStorage.setItem(TOKEN_KEY, access_token);
      setToken(access_token);
      return res.data;
   }, []);

   const register = useCallback(async (email, username, password) => {
      const res = await api.auth.register({ email, username, password });
      const { access_token } = res.data;
      localStorage.setItem(TOKEN_KEY, access_token);
      setToken(access_token);
      return res.data;
   }, []);

   const logout = useCallback(() => {
      localStorage.removeItem(TOKEN_KEY);
      setToken(null);
      setUser(null);
   }, []);

   return (
      <AuthContext.Provider value={{ token, user, loading, login, register, logout }}>
         {children}
      </AuthContext.Provider>
   );
}

export function useAuth() {
   const ctx = useContext(AuthContext);
   if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
   return ctx;
}
