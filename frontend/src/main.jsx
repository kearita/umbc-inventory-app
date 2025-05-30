import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter} from "react-router-dom"; // Provides routing for the app
import App from "./App"; // Provides routing for the app
import { AuthProvider } from "./contexts/AuthContext"; // Auth context for managing user state
import { CartProvider } from "./contexts/CartContext"; // Cart context for managing cart state

// Render the app
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <AuthProvider>
      {/* Provides user state to all components */}
      <BrowserRouter>
        <CartProvider>
          <App /> {/* Main app component */}
        </CartProvider>
      </BrowserRouter>
    </AuthProvider>
  </React.StrictMode>
);
