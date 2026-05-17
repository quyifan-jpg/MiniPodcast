import React, { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';

const Login = () => {
   const { login } = useAuth();
   const navigate = useNavigate();
   const location = useLocation();
   const params = new URLSearchParams(location.search);
   const redirect = params.get('redirect') || '/studio';

   const [email, setEmail] = useState('');
   const [password, setPassword] = useState('');
   const [showPassword, setShowPassword] = useState(false);
   const [submitting, setSubmitting] = useState(false);
   const [error, setError] = useState(null);

   const handleSubmit = async e => {
      e.preventDefault();
      setError(null);
      setSubmitting(true);
      try {
         await login({ email, password });
         navigate(redirect, { replace: true });
      } catch (err) {
         const msg =
            err?.response?.data?.message ||
            err?.response?.data?.detail ||
            err?.message ||
            'Login failed';
         setError(msg);
      } finally {
         setSubmitting(false);
      }
   };

   return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-b from-gray-900 to-black p-4">
         <div className="w-full max-w-md bg-gradient-to-br from-gray-800 to-gray-900 border border-gray-700 rounded-md shadow-2xl p-8">
            <h1 className="text-2xl font-bold text-white mb-2 text-center">Sign in</h1>
            <p className="text-gray-400 text-sm mb-6 text-center">
               Welcome back to AI Podcast Studio
            </p>
            {error && (
               <div className="mb-4 p-3 bg-red-900/30 border border-red-800/50 rounded text-red-400 text-sm">
                  {error}
               </div>
            )}
            <form onSubmit={handleSubmit} className="space-y-4">
               <div>
                  <label htmlFor="login-email" className="block text-sm text-gray-300 mb-1">
                     Email
                  </label>
                  <input
                     id="login-email"
                     type="email"
                     required
                     value={email}
                     onChange={e => setEmail(e.target.value)}
                     className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600"
                     autoComplete="email"
                  />
               </div>
               <div>
                  <label htmlFor="login-password" className="block text-sm text-gray-300 mb-1">
                     Password
                  </label>
                  <div className="relative">
                     <input
                        id="login-password"
                        type={showPassword ? 'text' : 'password'}
                        required
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        className="w-full px-3 py-2 pr-10 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600"
                        autoComplete="current-password"
                     />
                     <button
                        type="button"
                        onClick={() => setShowPassword(s => !s)}
                        aria-label={showPassword ? 'Hide password' : 'Show password'}
                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-200"
                     >
                        {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                     </button>
                  </div>
               </div>
               <button
                  type="submit"
                  disabled={submitting}
                  className={`w-full py-2.5 bg-gradient-to-r from-emerald-700 to-emerald-800 hover:from-emerald-600 hover:to-emerald-700 text-white font-medium rounded transition ${
                     submitting ? 'opacity-70 cursor-not-allowed' : ''
                  }`}
               >
                  {submitting ? 'Signing in…' : 'Sign in'}
               </button>
            </form>
            <p className="mt-6 text-sm text-gray-400 text-center">
               No account?{' '}
               <Link
                  to={`/register${location.search}`}
                  className="text-emerald-400 hover:text-emerald-300"
               >
                  Create one
               </Link>
            </p>
         </div>
      </div>
   );
};

export default Login;
