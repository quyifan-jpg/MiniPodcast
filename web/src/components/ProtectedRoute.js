import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';

export default function ProtectedRoute({ children }) {
   const { token, loading } = useAuth();
   const location = useLocation();

   if (loading) {
      return (
         <div className="min-h-screen bg-gradient-to-b from-gray-900 to-black flex items-center justify-center">
            <svg className="animate-spin h-8 w-8 text-emerald-400" fill="none" viewBox="0 0 24 24">
               <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
               <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
         </div>
      );
   }

   if (!token) {
      return <Navigate to="/login" state={{ from: location.pathname }} replace />;
   }

   return children;
}
