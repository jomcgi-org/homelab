import { mount } from "./mount";
import { HomePage } from "./pages";

// Each entry imports only its own page, so a route ships only its own code.
mount(<HomePage />);
