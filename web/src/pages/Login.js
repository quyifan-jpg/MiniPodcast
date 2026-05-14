import { useState } from 'react';
import { useNavigate, useLocation, Link } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

export default function Login() {
   const { login, register } = useAuth();
   const navigate = useNavigate();
   const location = useLocation();
   const from = location.state?.from || '/studio';

   const [tab, setTab]         = useState('login');   // 'login' | 'register'
   const [form, setForm]       = useState({ email: '', username: '', password: '' });
   const [error, setError]     = useState('');
   const [submitting, setSubmitting] = useState(false);

   const set = field => e => setForm(f => ({ ...f, [field]: e.target.value }));

   const handleSubmit = async e => {
      e.preventDefault();
      setError('');
      setSubmitting(true);
      try {
         if (tab === 'login') {
            await login(form.email, form.password);
         } else {
            await register(form.email, form.username, form.password);
         }
         navigate(from, { replace: true });
      } catch (err) {
         const msg = err.response?.data?.detail || 'Something went wrong. Please try again.';
         setError(Array.isArray(msg) ? msg.map(e => e.msg).join(', ') : msg);
      } finally {
         setSubmitting(false);
      }
   };

   return (
      <div className="min-h-screen bg-gradient-to-b from-gray-900 to-black flex items-center justify-center px-4">
         {/* Card */}
         <div className="w-full max-w-md">
            {/* Logo */}
            <div className="text-center mb-8">
               <Link to="/" className="inline-flex items-center gap-2">
                  <span style={{ fontSize: '2rem' }}>🦉</span>
                  <span className="text-2xl font-bold text-gray-100">
                     <span className="text-emerald-400">Mini</span>Podcast
                  </span>
               </Link>
               <p className="text-gray-500 text-sm mt-2">AI-powered podcast generation</p>
            </div>

            <div className="bg-gray-900 border border-gray-800 rounded-2xl shadow-2xl p-8">
               {/* Tabs */}
               <div className="flex mb-6 bg-gray-800 rounded-lg p-1">
                  {['login', 'register'].map(t => (
                     <button
                        key={t}
                        onClick={() => { setTab(t); setError(''); }}
                        className={`flex-1 py-2 rounded-md text-sm font-medium transition-all duration-200 ${
                           tab === t
                              ? 'bg-emerald-500 text-white shadow'
                              : 'text-gray-400 hover:text-gray-200'
                        }`}
                     >
                        {t === 'login' ? 'Sign In' : 'Create Account'}
                     </button>
                  ))}
               </div>

               <form onSubmit={handleSubmit} className="space-y-4">
                  {/* Email */}
                  <div>
                     <label className="block text-sm text-gray-400 mb-1">Email</label>
                     <input
                        type="email"
                        value={form.email}
                        onChange={set('email')}
                        required
                        placeholder="you@example.com"
                        className="w-full bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-4 py-2.5 text-sm
                                   focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500
                                   placeholder-gray-600 transition-colors"
                     />
                  </div>

                  {/* Username (register only) */}
                  {tab === 'register' && (
                     <div>
                        <label className="block text-sm text-gray-400 mb-1">Username</label>
                        <input
                           type="text"
                           value={form.username}
                           onChange={set('username')}
                           required
                           minLength={3}
                           placeholder="yourname"
                           className="w-full bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-4 py-2.5 text-sm
                                      focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500
                                      placeholder-gray-600 transition-colors"
                        />
                     </div>
                  )}

                  {/* Password */}
                  <div>
                     <label className="block text-sm text-gray-400 mb-1">Password</label>
                     <input
                        type="password"
                        value={form.password}
                        onChange={set('password')}
                        required
                        minLength={8}
                        placeholder="••••••••"
                        className="w-full bg-gray-800 border border-gray-700 text-gray-100 rounded-lg px-4 py-2.5 text-sm
                                   focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500
                                   placeholder-gray-600 transition-colors"
                     />
                     {tab === 'register' && (
                        <p className="text-xs text-gray-600 mt-1">At least 8 characters</p>
                     )}
                  </div>

                  {/* Error */}
                  {error && (
                     <div className="bg-red-900/30 border border-red-700/50 rounded-lg px-4 py-3 text-red-400 text-sm">
                        {error}
                     </div>
                  )}

                  {/* Submit */}
                  <button
                     type="submit"
                     disabled={submitting}
                     className="w-full py-2.5 bg-emerald-500 hover:bg-emerald-400 disabled:bg-emerald-800
                                disabled:cursor-not-allowed text-white font-semibold rounded-lg text-sm
                                transition-colors duration-200 flex items-center justify-center gap-2"
                  >
                     {submitting ? (
                        <>
                           <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                           </svg>
                           {tab === 'login' ? 'Signing in…' : 'Creating account…'}
                        </>
                     ) : (
                        tab === 'login' ? 'Sign In' : 'Create Account'
                     )}
                  </button>
               </form>
            </div>

            <p className="text-center text-gray-600 text-xs mt-6">
               {tab === 'login' ? "Don't have an account? " : 'Already have an account? '}
               <button
                  onClick={() => { setTab(tab === 'login' ? 'register' : 'login'); setError(''); }}
                  className="text-emerald-400 hover:text-emerald-300 transition-colors"
               >
                  {tab === 'login' ? 'Create one' : 'Sign in'}
               </button>
            </p>
         </div>
      </div>
   );
}
