import React from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

const ProtectedRoute = ({ children }) => {
   const { isAuthenticated, token, loading } = useAuth();
   const location = useLocation();

   // While we still have a token but haven't resolved the user yet, hold render
   // so a disabled / revoked account can't briefly see protected UI.
   if (loading || (token && !isAuthenticated)) {
      return (
         <div className="min-h-screen flex items-center justify-center bg-gray-900 text-gray-400">
            Loading…
         </div>
      );
   }

   if (!isAuthenticated) {
      const redirect = encodeURIComponent(location.pathname + location.search);
      return <Navigate to={`/login?redirect=${redirect}`} replace />;
   }

   return children;
};

export default ProtectedRoute;
