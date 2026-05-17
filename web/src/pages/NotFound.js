import React from 'react';
import { Link } from 'react-router-dom';

const NotFound = () => (
   <div className="min-h-[60vh] flex flex-col items-center justify-center text-center px-4">
      <div className="text-7xl mb-3" aria-hidden>
         🦉
      </div>
      <h1 className="text-3xl font-bold text-white mb-2">404 — page not found</h1>
      <p className="text-gray-400 mb-6 max-w-md">
         The page you're looking for doesn't exist or has moved.
      </p>
      <div className="flex gap-3">
         <Link
            to="/"
            className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 text-white rounded transition-colors"
         >
            Go home
         </Link>
         <Link
            to="/studio"
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 rounded transition-colors"
         >
            Open studio
         </Link>
      </div>
   </div>
);

export default NotFound;
