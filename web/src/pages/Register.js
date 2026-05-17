import React, { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';

const Register = () => {
   const { register } = useAuth();
   const navigate = useNavigate();
   const location = useLocation();
   const params = new URLSearchParams(location.search);
   const redirect = params.get('redirect') || '/studio';

   const [email, setEmail] = useState('');
   const [username, setUsername] = useState('');
   const [password, setPassword] = useState('');
   const [confirmPassword, setConfirmPassword] = useState('');
   const [showPassword, setShowPassword] = useState(false);
   const [submitting, setSubmitting] = useState(false);
   const [error, setError] = useState(null);

   const handleSubmit = async e => {
      e.preventDefault();
      setError(null);
      if (password.length < 8) {
         setError('Password must be at least 8 characters');
         return;
      }
      if (password !== confirmPassword) {
         setError('Passwords do not match');
         return;
      }
      setSubmitting(true);
      try {
         await register({ email, username, password });
         navigate(redirect, { replace: true });
      } catch (err) {
         const msg =
            err?.response?.data?.message ||
            err?.response?.data?.detail ||
            err?.message ||
            'Registration failed';
         setError(msg);
      } finally {
         setSubmitting(false);
      }
   };

   return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-b from-gray-900 to-black p-4">
         <div className="w-full max-w-md bg-gradient-to-br from-gray-800 to-gray-900 border border-gray-700 rounded-md shadow-2xl p-8">
            <h1 className="text-2xl font-bold text-white mb-2 text-center">Create account</h1>
            <p className="text-gray-400 text-sm mb-6 text-center">
               Start building podcasts in minutes
            </p>
            {error && (
               <div className="mb-4 p-3 bg-red-900/30 border border-red-800/50 rounded text-red-400 text-sm">
                  {error}
               </div>
            )}
            <form onSubmit={handleSubmit} className="space-y-4">
               <div>
                  <label htmlFor="reg-email" className="block text-sm text-gray-300 mb-1">
                     Email
                  </label>
                  <input
                     id="reg-email"
                     type="email"
                     required
                     value={email}
                     onChange={e => setEmail(e.target.value)}
                     className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600"
                     autoComplete="email"
                  />
               </div>
               <div>
                  <label htmlFor="reg-username" className="block text-sm text-gray-300 mb-1">
                     Username
                  </label>
                  <input
                     id="reg-username"
                     type="text"
                     required
                     minLength={3}
                     maxLength={50}
                     value={username}
                     onChange={e => setUsername(e.target.value)}
                     className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600"
                     autoComplete="username"
                  />
                  <p className="mt-1 text-xs text-gray-500">
                     Letters, digits, _ and - only. 3–50 chars.
                  </p>
               </div>
               <div>
                  <label htmlFor="reg-password" className="block text-sm text-gray-300 mb-1">
                     Password
                  </label>
                  <div className="relative">
                     <input
                        id="reg-password"
                        type={showPassword ? 'text' : 'password'}
                        required
                        minLength={8}
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        className="w-full px-3 py-2 pr-10 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600"
                        autoComplete="new-password"
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
                  <p className="mt-1 text-xs text-gray-500">Minimum 8 characters.</p>
               </div>
               <div>
                  <label htmlFor="reg-confirm" className="block text-sm text-gray-300 mb-1">
                     Confirm password
                  </label>
                  <input
                     id="reg-confirm"
                     type={showPassword ? 'text' : 'password'}
                     required
                     value={confirmPassword}
                     onChange={e => setConfirmPassword(e.target.value)}
                     className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600"
                     autoComplete="new-password"
                  />
               </div>
               <button
                  type="submit"
                  disabled={submitting}
                  className={`w-full py-2.5 bg-gradient-to-r from-emerald-700 to-emerald-800 hover:from-emerald-600 hover:to-emerald-700 text-white font-medium rounded transition ${
                     submitting ? 'opacity-70 cursor-not-allowed' : ''
                  }`}
               >
                  {submitting ? 'Creating…' : 'Create account'}
               </button>
            </form>
            <p className="mt-6 text-sm text-gray-400 text-center">
               Already have an account?{' '}
               <Link
                  to={`/login${location.search}`}
                  className="text-emerald-400 hover:text-emerald-300"
               >
                  Sign in
               </Link>
            </p>
         </div>
      </div>
   );
};

export default Register;
