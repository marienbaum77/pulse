import { Link } from "react-router-dom";
import { Empty } from "./ui";

export function NoProject() {
  return (
    <Empty
      title="Пока нет ни одного проекта"
      text="Проект — это тема, источники и правила, по которым Pulse ищет сюжеты и готовит посты."
      action={<Link className="btn btn-primary" to="/project?new=1">Создать проект</Link>}
    />
  );
}
