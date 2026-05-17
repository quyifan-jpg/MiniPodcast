import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import api from '../services/api';
import { useAuth } from '../contexts/AuthContext';

const sectionClass =
   'bg-gradient-to-br from-gray-800/60 to-gray-900/60 border border-gray-700/40 rounded-md p-6 shadow';
const inputClass =
   'w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-emerald-600';
const labelClass = 'block text-sm text-gray-300 mb-1';

const Banner = ({ kind, children }) =>
   children ? (
      <div
         className={`mb-4 p-3 rounded text-sm border ${
            kind === 'error'
               ? 'bg-red-900/30 border-red-800/50 text-red-400'
               : 'bg-emerald-900/30 border-emerald-800/50 text-emerald-300'
         }`}
         role="alert"
      >
         {children}
      </div>
   ) : null;

const ProfileSection = ({ user, onUpdated }) => {
   const [username, setUsername] = useState(user?.username || '');
   const [email, setEmail] = useState(user?.email || '');
   const [submitting, setSubmitting] = useState(false);
   const [err, setErr] = useState(null);
   const [ok, setOk] = useState(null);

   useEffect(() => {
      setUsername(user?.username || '');
      setEmail(user?.email || '');
   }, [user]);

   const dirty = username !== user?.username || email !== user?.email;

   const handleSubmit = async e => {
      e.preventDefault();
      setErr(null);
      setOk(null);
      setSubmitting(true);
      try {
         const payload = {};
         if (username !== user?.username) payload.username = username;
         if (email !== user?.email) payload.email = email;
         await api.auth.updateProfile(payload);
         await onUpdated();
         setOk('Profile updated.');
      } catch (e2) {
         setErr(
            e2?.response?.data?.message ||
               e2?.response?.data?.detail ||
               e2?.message ||
               'Update failed'
         );
      } finally {
         setSubmitting(false);
      }
   };

   return (
      <section className={sectionClass}>
         <h2 className="text-lg font-semibold text-white mb-4">Profile</h2>
         <Banner kind="error">{err}</Banner>
         <Banner kind="ok">{ok}</Banner>
         <form onSubmit={handleSubmit} className="space-y-4">
            <div>
               <label htmlFor="acc-username" className={labelClass}>
                  Username
               </label>
               <input
                  id="acc-username"
                  type="text"
                  required
                  minLength={3}
                  maxLength={50}
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  className={inputClass}
                  autoComplete="username"
               />
            </div>
            <div>
               <label htmlFor="acc-email" className={labelClass}>
                  Email
               </label>
               <input
                  id="acc-email"
                  type="email"
                  required
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  className={inputClass}
                  autoComplete="email"
               />
            </div>
            <button
               type="submit"
               disabled={!dirty || submitting}
               className={`px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-white rounded transition ${
                  !dirty || submitting ? 'opacity-50 cursor-not-allowed' : ''
               }`}
            >
               {submitting ? 'Saving…' : 'Save changes'}
            </button>
         </form>
      </section>
   );
};

const PasswordSection = () => {
   const [currentPassword, setCurrentPassword] = useState('');
   const [newPassword, setNewPassword] = useState('');
   const [confirm, setConfirm] = useState('');
   const [show, setShow] = useState(false);
   const [submitting, setSubmitting] = useState(false);
   const [err, setErr] = useState(null);
   const [ok, setOk] = useState(null);

   const handleSubmit = async e => {
      e.preventDefault();
      setErr(null);
      setOk(null);
      if (newPassword.length < 8) {
         setErr('New password must be at least 8 characters');
         return;
      }
      if (newPassword !== confirm) {
         setErr('Passwords do not match');
         return;
      }
      setSubmitting(true);
      try {
         await api.auth.changePassword({ currentPassword, newPassword });
         setCurrentPassword('');
         setNewPassword('');
         setConfirm('');
         setOk('Password updated.');
      } catch (e2) {
         setErr(
            e2?.response?.data?.message ||
               e2?.response?.data?.detail ||
               e2?.message ||
               'Password change failed'
         );
      } finally {
         setSubmitting(false);
      }
   };

   const inputType = show ? 'text' : 'password';

   return (
      <section className={sectionClass}>
         <h2 className="text-lg font-semibold text-white mb-4">Change password</h2>
         <Banner kind="error">{err}</Banner>
         <Banner kind="ok">{ok}</Banner>
         <form onSubmit={handleSubmit} className="space-y-4">
            <div>
               <label htmlFor="acc-current" className={labelClass}>
                  Current password
               </label>
               <div className="relative">
                  <input
                     id="acc-current"
                     type={inputType}
                     required
                     value={currentPassword}
                     onChange={e => setCurrentPassword(e.target.value)}
                     className={`${inputClass} pr-10`}
                     autoComplete="current-password"
                  />
                  <button
                     type="button"
                     onClick={() => setShow(s => !s)}
                     aria-label={show ? 'Hide password' : 'Show password'}
                     className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-200"
                  >
                     {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
               </div>
            </div>
            <div>
               <label htmlFor="acc-new" className={labelClass}>
                  New password
               </label>
               <input
                  id="acc-new"
                  type={inputType}
                  required
                  minLength={8}
                  value={newPassword}
                  onChange={e => setNewPassword(e.target.value)}
                  className={inputClass}
                  autoComplete="new-password"
               />
               <p className="mt-1 text-xs text-gray-500">Minimum 8 characters.</p>
            </div>
            <div>
               <label htmlFor="acc-confirm" className={labelClass}>
                  Confirm new password
               </label>
               <input
                  id="acc-confirm"
                  type={inputType}
                  required
                  value={confirm}
                  onChange={e => setConfirm(e.target.value)}
                  className={inputClass}
                  autoComplete="new-password"
               />
            </div>
            <button
               type="submit"
               disabled={submitting}
               className={`px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-white rounded transition ${
                  submitting ? 'opacity-50 cursor-not-allowed' : ''
               }`}
            >
               {submitting ? 'Updating…' : 'Update password'}
            </button>
         </form>
      </section>
   );
};

const DangerSection = ({ user, onDeleted }) => {
   const [confirming, setConfirming] = useState(false);
   const [typed, setTyped] = useState('');
   const [submitting, setSubmitting] = useState(false);
   const [err, setErr] = useState(null);

   const handleDelete = async () => {
      setErr(null);
      setSubmitting(true);
      try {
         await api.auth.deactivateAccount();
         onDeleted();
      } catch (e2) {
         setErr(
            e2?.response?.data?.message ||
               e2?.response?.data?.detail ||
               e2?.message ||
               'Delete failed'
         );
         setSubmitting(false);
      }
   };

   return (
      <section
         className={`${sectionClass} border-red-900/40`}
         style={{ borderColor: 'rgba(127, 29, 29, 0.4)' }}
      >
         <h2 className="text-lg font-semibold text-red-400 mb-2">Danger zone</h2>
         <p className="text-sm text-gray-400 mb-4">
            Deactivating your account will sign you out immediately and prevent future logins.
            Existing data is preserved but no longer accessible.
         </p>
         {!confirming ? (
            <button
               onClick={() => setConfirming(true)}
               className="px-4 py-2 bg-red-700 hover:bg-red-600 text-white rounded transition"
            >
               Deactivate account
            </button>
         ) : (
            <div className="space-y-3">
               <Banner kind="error">{err}</Banner>
               <p className="text-sm text-gray-300">
                  Type your username <span className="font-mono text-red-300">{user?.username}</span>{' '}
                  to confirm.
               </p>
               <input
                  type="text"
                  value={typed}
                  onChange={e => setTyped(e.target.value)}
                  className={inputClass}
                  aria-label="Confirm username"
               />
               <div className="flex gap-2">
                  <button
                     onClick={handleDelete}
                     disabled={typed !== user?.username || submitting}
                     className={`px-4 py-2 bg-red-700 text-white rounded transition ${
                        typed !== user?.username || submitting
                           ? 'opacity-50 cursor-not-allowed'
                           : 'hover:bg-red-600'
                     }`}
                  >
                     {submitting ? 'Deactivating…' : 'I understand, deactivate'}
                  </button>
                  <button
                     onClick={() => {
                        setConfirming(false);
                        setTyped('');
                        setErr(null);
                     }}
                     className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 rounded transition"
                  >
                     Cancel
                  </button>
               </div>
            </div>
         )}
      </section>
   );
};

const Account = () => {
   const { user, refreshUser, logout } = useAuth();
   const navigate = useNavigate();

   const handleDeleted = () => {
      logout();
      navigate('/', { replace: true });
   };

   if (!user) {
      return <div className="text-gray-400 py-8 text-center">Loading account…</div>;
   }

   return (
      <div className="max-w-2xl mx-auto py-6 space-y-6">
         <div>
            <h1 className="text-2xl font-bold text-white">Account settings</h1>
            <p className="text-sm text-gray-400 mt-1">
               Manage your profile, password and account status.
            </p>
         </div>
         <ProfileSection user={user} onUpdated={refreshUser} />
         <PasswordSection />
         <DangerSection user={user} onDeleted={handleDeleted} />
      </div>
   );
};

export default Account;
