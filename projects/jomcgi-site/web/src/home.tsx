import { mount } from "./mount";
import { HomePage } from "./home-page";

// Each entry imports only its own page, so a route ships only its own code.
mount(<HomePage />);
