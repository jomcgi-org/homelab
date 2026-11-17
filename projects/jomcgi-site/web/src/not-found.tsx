import { mount } from "./mount";
import { PAGES } from "./pages";

const Page = PAGES["404.html"]!;
mount(<Page />);
