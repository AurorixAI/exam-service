"""
ALGO V1 - Exam Generator (Модуль 2)
Генератор тестов и экзаменов из оригинальных задач учебников.

ВАЖНО: Основной режим — использование ОРИГИНАЛЬНЫХ задач из учебников.
Генерация вариантов через шаблоны — ДОПОЛНИТЕЛЬНАЯ опция.

Учитель:
1. Выбирает учебник → параграфы → задачи
2. Формирует тест из оригинальных задач
3. (Опционально) Генерирует варианты с другими числами
"""

import json
import random
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid



# Порог для жёстких зависимостей (аналогично professional_engine)
HARD_PREREQ_WEIGHT = 7.0


class ExamType(Enum):
    """Тип теста"""

    HOMEWORK = "homework"  # Домашняя работа
    QUIZ = "quiz"  # Мини-тест
    TEST = "test"  # Контрольная работа
    EXAM = "exam"  # Экзамен
    DIAGNOSTIC = "diagnostic"  # Диагностика (Модуль 1)


class TaskDifficulty(Enum):
    """Сложность задачи"""

    A = "basic"  # Базовый уровень
    B = "standard"  # Стандартный
    C = "advanced"  # Продвинутый


class VariantMode(Enum):
    """Режим создания вариантов"""

    ORIGINAL = "original"  # Только оригинальные задачи
    SHUFFLE = "shuffle"  # Оригинальные, перемешанные
    TEMPLATE = "template"  # Генерация через шаблоны (доп. опция)


class SourceMode(Enum):
    """Режим источника задач"""

    BY_CURRICULUM = "by_curriculum"  # По программе (L1→L4 из knowledge_hierarchy)
    BY_TEXTBOOK = "by_textbook"  # По учебнику (точная копия TOC)


@dataclass
class TaskInfo:
    """Информация о задаче"""

    task_id: str
    textbook_id: str
    paragraph: str
    task_number: str

    skill_id: Optional[str]  # L4 атомарный навык (None для textbook-only)
    difficulty: TaskDifficulty

    problem_text: str  # Условие
    answer: str  # Ответ
    solution: Optional[str] = None  # Решение

    # Для генерации вариантов
    template_id: Optional[str] = None
    has_template: bool = False

    @property
    def has_core_mapping(self) -> bool:
        """Есть ли привязка к ядру (knowledge_hierarchy)."""
        return self.skill_id is not None


@dataclass
class PrerequisiteWarning:
    """Предупреждение о нарушении зависимостей при генерации теста"""

    skill_id: str  # Навык в тесте
    skill_name: str
    missing_prereq_id: str  # Отсутствующий пререквизит
    missing_prereq_name: str
    dependency_type: str  # 'hard' или 'soft'
    weight: float


@dataclass
class ExamConfig:
    """
    Конфигурация теста.

    Два режима источника:
    - BY_CURRICULUM: учитель выбирает L1-L4 навыки из единого дерева,
      система автоматически подбирает задачи по метаданным
    - BY_TEXTBOOK: учитель выбирает учебник + параграфы/разделы,
      задачи берутся из оцифрованного учебника
    """

    title: str
    exam_type: ExamType

    # Режим источника
    source_mode: SourceMode = SourceMode.BY_TEXTBOOK

    # BY_TEXTBOOK: конкретный учебник
    textbook_id: Optional[str] = None
    paragraphs: List[str] = field(default_factory=list)
    structure_ids: List[int] = field(default_factory=list)  # ID из textbook_structure

    # BY_CURRICULUM: навыки из knowledge_hierarchy
    skills: List[str] = field(default_factory=list)  # L4 навыки
    class_level: Optional[int] = None

    # Фильтры (общие)
    difficulty_levels: List[TaskDifficulty] = field(default_factory=list)

    # Количество
    tasks_count: int = 10
    time_minutes: int = 45

    # Варианты
    variant_mode: VariantMode = VariantMode.ORIGINAL
    variants_count: int = 1

    # Баллы
    points_per_task: Dict[str, int] = field(default_factory=dict)
    total_points: int = 100


@dataclass
class ExamVariant:
    """Вариант теста"""

    variant_id: str
    variant_number: int

    tasks: List[TaskInfo]

    total_points: int
    time_minutes: int

    created_at: datetime = field(default_factory=datetime.now)

    # Метаданные
    seed: Optional[int] = None  # Для воспроизводимости


@dataclass
class GeneratedExam:
    """Сгенерированный тест"""

    exam_id: str
    config: ExamConfig

    variants: List[ExamVariant]

    created_at: datetime = field(default_factory=datetime.now)
    created_by: Optional[str] = None

    # Статистика
    skills_covered: Set[str] = field(default_factory=set)
    difficulty_distribution: Dict[str, int] = field(default_factory=dict)

    # Предупреждения о зависимостях
    prerequisite_warnings: List[PrerequisiteWarning] = field(default_factory=list)


class TaskRepository:
    """
    Репозиторий задач из учебников.
    В реальном приложении работает с БД.
    """

    def __init__(self, db_connection=None):
        self.db = db_connection
        self._cache: Dict[str, TaskInfo] = {}

    def get_tasks_by_paragraph(
        self, textbook_id: str, paragraph: str
    ) -> List[TaskInfo]:
        """Получает все задачи из параграфа"""

        # SQL запрос (псевдокод)

        # Пример данных для демонстрации
        return self._get_demo_tasks(textbook_id, paragraph)

    def get_tasks_by_skills(
        self,
        textbook_id: str,
        skills: List[str],
        difficulty: List[TaskDifficulty] = None,
    ) -> List[TaskInfo]:
        """Получает задачи по навыкам"""

        # В реальности — SQL с фильтрами
        all_tasks = []
        for para in ["1.1", "1.2", "2.1", "2.2"]:
            all_tasks.extend(self._get_demo_tasks(textbook_id, para))

        result = [t for t in all_tasks if t.skill_id in skills]

        if difficulty:
            result = [t for t in result if t.difficulty in difficulty]

        return result

    def _get_demo_tasks(self, textbook_id: str, paragraph: str) -> List[TaskInfo]:
        """Stub — возвращает пустой список. Реальные задачи загружаются через DB."""
        return []


class ExamGenerator:
    """
    Главный класс генерации тестов.

    Основной режим: оригинальные задачи из учебника.
    Дополнительная опция: генерация вариантов через шаблоны.
    """

    def __init__(self, task_repository: TaskRepository = None, db_session=None):
        self.task_repo = task_repository or TaskRepository()
        self.db = db_session  # SQLAlchemy session for BY_CURRICULUM mode

    def generate_exam(self, config: ExamConfig) -> GeneratedExam:
        """
        Генерирует тест по конфигурации.

        Включает валидацию пререквизитов: проверяет, что все зависимости
        выбранных навыков покрыты. Предупреждения включаются в результат.

        Args:
            config: ExamConfig с параметрами теста

        Returns:
            GeneratedExam с вариантами и prerequisite_warnings
        """

        exam_id = str(uuid.uuid4())

        # ШАГ 1: Собираем пул оригинальных задач
        task_pool = self._collect_task_pool(config)

        if len(task_pool) < config.tasks_count:
            print(
                f"⚠️ Недостаточно задач: найдено {len(task_pool)}, нужно {config.tasks_count}"
            )

        # ШАГ 2: Проверяем зависимости (prereq validation)
        prereq_warnings = []
        if config.source_mode == SourceMode.BY_CURRICULUM and config.skills:
            prereq_warnings = self.validate_exam_prerequisites(config.skills)
            if prereq_warnings:
                hard_count = sum(
                    1 for w in prereq_warnings if w.dependency_type == "hard"
                )
                soft_count = len(prereq_warnings) - hard_count
                print(
                    f"⚠️ Зависимости: {hard_count} жёстких, {soft_count} мягких непокрытых пререквизитов"
                )

        # ШАГ 3: Генерируем варианты
        variants = []

        for i in range(config.variants_count):
            variant = self._create_variant(
                config=config, task_pool=task_pool, variant_number=i + 1
            )
            variants.append(variant)

        # ШАГ 3: Собираем статистику
        skills_covered = set()
        difficulty_dist = {d.value: 0 for d in TaskDifficulty}

        for variant in variants:
            for task in variant.tasks:
                if task.skill_id:
                    skills_covered.add(task.skill_id)
                difficulty_dist[task.difficulty.value] += 1

        return GeneratedExam(
            exam_id=exam_id,
            config=config,
            variants=variants,
            skills_covered=skills_covered,
            difficulty_distribution=difficulty_dist,
            prerequisite_warnings=prereq_warnings,
        )

    def _collect_task_pool(self, config: ExamConfig) -> List[TaskInfo]:
        """
        Собирает пул задач в зависимости от режима источника.

        BY_TEXTBOOK: задачи из конкретного учебника (по параграфам/разделам)
        BY_CURRICULUM: задачи по навыкам из любого учебника (автоподбор)
        """

        pool = []

        if config.source_mode == SourceMode.BY_TEXTBOOK:
            # Режим 1: По учебнику — задачи из конкретной книги
            if config.paragraphs:
                for para in config.paragraphs:
                    tasks = self.task_repo.get_tasks_by_paragraph(
                        config.textbook_id, para
                    )
                    pool.extend(tasks)

            elif config.structure_ids:
                # По structure_id из TOC (новый способ)
                for sid in config.structure_ids:
                    tasks = self._get_tasks_by_structure(config.textbook_id, sid)
                    pool.extend(tasks)

            elif config.skills:
                pool = self.task_repo.get_tasks_by_skills(
                    config.textbook_id, config.skills, config.difficulty_levels
                )

        elif config.source_mode == SourceMode.BY_CURRICULUM:
            # Режим 2: По программе — задачи из всех источников по навыкам
            if config.skills:
                pool = self._get_tasks_by_curriculum(
                    config.skills, config.class_level, config.difficulty_levels
                )
            else:
                # Если навыки не указаны, берём все навыки для класса
                pool = self._get_tasks_for_class(
                    config.class_level or 5, config.difficulty_levels
                )

        # Фильтруем по сложности
        if config.difficulty_levels:
            pool = [t for t in pool if t.difficulty in config.difficulty_levels]

        return pool

    def _get_tasks_by_structure(
        self, textbook_id: str, structure_id: int
    ) -> List[TaskInfo]:
        """Получает задачи по structure_id (из TOC).

        Включает задачи без skill_id (textbook-only) —
        LEFT JOIN на knowledge_hierarchy.
        """
        if not self.db:
            return []

        from sqlalchemy import text

        result = self.db.execute(
            text("""
                SELECT tt.task_id, tt.exercise_number,
                       tm.skill_id, tm.difficulty, tm.question_text,
                       tm.correct_answer, tm.question_latex,
                       FALSE AS has_template
                FROM textbook_tasks tt
                JOIN tasks_master tm ON tt.task_id = tm.id
                WHERE tt.textbook_id = :textbook_id
                AND (tm.toc_id = :sid
                     OR tm.toc_id IN (
                         SELECT id FROM textbook_toc WHERE parent_id = :sid
                     ))
                AND tm.is_active = TRUE
                ORDER BY tt.exercise_number
            """),
            {"textbook_id": textbook_id, "sid": structure_id},
        )

        return [
            TaskInfo(
                task_id=r["task_id"],
                textbook_id=textbook_id,
                paragraph=str(structure_id),
                task_number=r.get("exercise_number", ""),
                skill_id=r.get("skill_id"),  # может быть None
                difficulty=self._map_difficulty(r.get("difficulty", "B")),
                problem_text=r.get("question_text", ""),
                answer=r.get("correct_answer", ""),
                has_template=r.get("has_template", False),
            )
            for r in result.mappings().all()
        ]

    def _get_tasks_by_curriculum(
        self,
        skills: List[str],
        class_level: Optional[int],
        difficulty_levels: List[TaskDifficulty],
        student_mastery: Optional[Dict[str, float]] = None,
    ) -> List[TaskInfo]:
        """Режим BY_CURRICULUM: задачи из любого источника по навыкам.

        Args:
            student_mastery: {скилл_id: mastery_probability}.
                Если передано, фильтрует задачи, чьи жёсткие
                зависимости (weight >= 7) не освоены учеником.
        """
        if not self.db:
            return self.task_repo.get_tasks_by_skills("", skills, difficulty_levels)

        from sqlalchemy import text

        diff_filter = ""
        params: dict = {"skills": tuple(skills)}

        if difficulty_levels:
            diff_values = [
                d.value[0].upper() if len(d.value) > 1 else d.value
                for d in difficulty_levels
            ]
            diff_filter = " AND tm.difficulty IN :diffs"
            params["diffs"] = tuple(diff_values)

        # Основной запрос с LEFT JOIN на skill_prerequisites
        # для получения информации о зависимостях
        result = self.db.execute(
            text(f"""
                SELECT tm.id, tm.skill_id, tm.difficulty, tm.question_text,
                       tm.correct_answer, tm.question_latex, FALSE AS has_template
                FROM tasks_master tm
                WHERE tm.skill_id IN :skills
                AND tm.is_active = TRUE
                AND tm.verification_status IN ('verified', 'pending')
                {diff_filter}
                ORDER BY RANDOM()
                LIMIT 200
            """),
            params,
        )

        tasks = [
            TaskInfo(
                task_id=r["id"],
                textbook_id="",
                paragraph="",
                task_number="",
                skill_id=r["skill_id"],
                difficulty=self._map_difficulty(r.get("difficulty", "B")),
                problem_text=r.get("question_text", ""),
                answer=r.get("correct_answer", ""),
                has_template=r.get("has_template", False),
            )
            for r in result.mappings().all()
        ]

        # Prereq-filter по данным mastery ученика (если предоставлены)
        if student_mastery and tasks:
            tasks = self._filter_tasks_by_mastery(tasks, student_mastery)

        return tasks

    def _get_tasks_for_class(
        self, class_level: int, difficulty_levels: List[TaskDifficulty]
    ) -> List[TaskInfo]:
        """Все задачи для класса (только с маппингом на ядро, BY_CURRICULUM)"""
        if not self.db:
            return []

        from sqlalchemy import text

        result = self.db.execute(
            text("""
                SELECT tm.id, tm.skill_id, tm.difficulty, tm.question_text,
                       tm.correct_answer, FALSE AS has_template
                FROM tasks_master tm
                JOIN knowledge_hierarchy kh ON tm.skill_id = kh.id
                WHERE tm.skill_id IS NOT NULL
                AND kh.class_level_start <= :cl
                AND (kh.class_level_end IS NULL OR kh.class_level_end >= :cl)
                AND tm.is_active = TRUE
                ORDER BY RANDOM()
                LIMIT 200
            """),
            {"cl": class_level},
        )

        return [
            TaskInfo(
                task_id=r["id"],
                textbook_id="",
                paragraph="",
                task_number="",
                skill_id=r["skill_id"],
                difficulty=self._map_difficulty(r.get("difficulty", "B")),
                problem_text=r.get("question_text", ""),
                answer=r.get("correct_answer", ""),
                has_template=r.get("has_template", False),
            )
            for r in result.mappings().all()
        ]

    @staticmethod
    def _map_difficulty(d: str) -> TaskDifficulty:
        return {
            "A": TaskDifficulty.A,
            "B": TaskDifficulty.B,
            "C": TaskDifficulty.C,
        }.get(d, TaskDifficulty.B)

    # =========================================================================
    # PREREQUISITE VALIDATION
    # =========================================================================

    def validate_exam_prerequisites(
        self, skills: List[str]
    ) -> List[PrerequisiteWarning]:
        """
        Проверяет, покрыты ли все зависимости выбранных навыков.

        Возвращает предупреждения о пререквизитах, которые требуются задачами
        теста, но не включены в набор навыков.

        Args:
            skills: Список L4 навыков, включённых в тест

        Returns:
            Список PrerequisiteWarning для каждой непокрытой зависимости
        """
        if not self.db or not skills:
            return []

        from sqlalchemy import text

        try:
            result = self.db.execute(
                text("""
                    SELECT sp.skill_id,
                           kh1.name_ru AS skill_name,
                           sp.prerequisite_id,
                           kh2.name_ru AS prereq_name,
                           sp.dependency_type,
                           COALESCE(sp.weight, 5) AS weight
                    FROM skill_prerequisites sp
                    JOIN knowledge_hierarchy kh1 ON sp.skill_id = kh1.id
                    JOIN knowledge_hierarchy kh2 ON sp.prerequisite_id = kh2.id
                    WHERE sp.skill_id IN :skills
                      AND sp.prerequisite_id NOT IN :skills
                    ORDER BY sp.weight DESC
                """),
                {"skills": tuple(skills)},
            )

            warnings = []
            for r in result.mappings().all():
                w = float(r["weight"])
                warnings.append(
                    PrerequisiteWarning(
                        skill_id=r["skill_id"],
                        skill_name=r["skill_name"] or r["skill_id"],
                        missing_prereq_id=r["prerequisite_id"],
                        missing_prereq_name=r["prereq_name"] or r["prerequisite_id"],
                        dependency_type="hard" if w >= HARD_PREREQ_WEIGHT else "soft",
                        weight=w,
                    )
                )

            return warnings
        except Exception as e:
            # Не блокируем генерацию при ошибке валидации
            import logging

            logging.getLogger(__name__).warning(f"Prereq validation failed: {e}")
            return []

    def _filter_tasks_by_mastery(
        self,
        tasks: List[TaskInfo],
        student_mastery: Dict[str, float],
        gap_threshold: float = 0.4,
    ) -> List[TaskInfo]:
        """
        Фильтрует задачи по mastery ученика: исключает задачи,
        чьи жёсткие зависимости (weight >= 7) не освоены.

        Args:
            tasks: Список задач
            student_mastery: {skill_id: mastery_probability}
            gap_threshold: Порог пробела
        """
        if not self.db:
            return tasks

        from sqlalchemy import text

        # Собираем skill_ids из задач
        skill_ids = list({t.skill_id for t in tasks if t.skill_id})
        if not skill_ids:
            return tasks

        try:
            # Получаем жёсткие зависимости для этих навыков
            result = self.db.execute(
                text("""
                    SELECT skill_id, prerequisite_id, COALESCE(weight, 5) AS weight
                    FROM skill_prerequisites
                    WHERE skill_id IN :skills
                      AND COALESCE(weight, 5) >= :hard_weight
                """),
                {"skills": tuple(skill_ids), "hard_weight": HARD_PREREQ_WEIGHT},
            )

            # {skill_id: [hard_prereq_ids]}
            hard_prereqs: Dict[str, List[str]] = {}
            for r in result.mappings().all():
                sid = r["skill_id"]
                hard_prereqs.setdefault(sid, []).append(r["prerequisite_id"])

            if not hard_prereqs:
                return tasks

            # Фильтруем: убираем задачи, чьи жёсткие пререквизиты провалены
            filtered = []
            for task in tasks:
                prereqs = hard_prereqs.get(task.skill_id, [])
                blocked = False
                for prereq_id in prereqs:
                    mastery = student_mastery.get(prereq_id)
                    if mastery is not None and mastery < gap_threshold:
                        blocked = True
                        break
                if not blocked:
                    filtered.append(task)

            return filtered if filtered else tasks  # Fallback: не оставляем пустым
        except Exception:
            return tasks

    def _create_variant(
        self, config: ExamConfig, task_pool: List[TaskInfo], variant_number: int
    ) -> ExamVariant:
        """Создает один вариант теста"""

        variant_id = str(uuid.uuid4())
        seed = variant_number * 1000 + hash(config.title) % 1000
        rng = random.Random(seed)

        selected_tasks = []

        if config.variant_mode == VariantMode.ORIGINAL:
            # ОСНОВНОЙ РЕЖИМ: Оригинальные задачи
            selected_tasks = self._select_original_tasks(
                task_pool, config.tasks_count, rng=rng
            )

        elif config.variant_mode == VariantMode.SHUFFLE:
            # Перемешанные оригинальные задачи
            selected_tasks = self._select_original_tasks(
                task_pool, config.tasks_count, shuffle=True, rng=rng
            )

        elif config.variant_mode == VariantMode.TEMPLATE:
            # ДОПОЛНИТЕЛЬНАЯ ОПЦИЯ: Генерация через шаблоны
            selected_tasks = self._generate_template_variant(
                task_pool, config.tasks_count, seed=seed
            )

        # Распределяем баллы
        points_per_task = config.points_per_task
        if not points_per_task:
            if selected_tasks:
                # Равномерное распределение
                base_points = config.total_points // len(selected_tasks)
                points_per_task = {t.task_id: base_points for t in selected_tasks}
            else:
                points_per_task = {}

        total_points = sum(points_per_task.get(t.task_id, 0) for t in selected_tasks)

        return ExamVariant(
            variant_id=variant_id,
            variant_number=variant_number,
            tasks=selected_tasks,
            total_points=total_points,
            time_minutes=config.time_minutes,
            seed=seed,
        )

    def _select_original_tasks(
        self,
        pool: List[TaskInfo],
        count: int,
        shuffle: bool = False,
        rng: random.Random = None,
    ) -> List[TaskInfo]:
        """Выбирает оригинальные задачи из пула"""
        if rng is None:
            rng = random.Random()

        available = list(pool)

        if shuffle:
            rng.shuffle(available)

        # Балансируем по сложности
        selected = []
        by_difficulty = {d: [] for d in TaskDifficulty}

        for task in available:
            by_difficulty[task.difficulty].append(task)

        # Примерное распределение: 30% A, 50% B, 20% C
        targets = {
            TaskDifficulty.A: int(count * 0.3),
            TaskDifficulty.B: int(count * 0.5),
            TaskDifficulty.C: count - int(count * 0.3) - int(count * 0.5),
        }

        for diff, target in targets.items():
            take = min(target, len(by_difficulty[diff]))
            selected.extend(by_difficulty[diff][:take])

        # Добиваем если не хватает
        while len(selected) < count and len(selected) < len(pool):
            for task in available:
                if task not in selected:
                    selected.append(task)
                    break

        return selected[:count]

    def _generate_template_variant(
        self, pool: List[TaskInfo], count: int, seed: int
    ) -> List[TaskInfo]:
        """Генерирует вариант через шаблоны (дополнительная опция)"""

        result = []

        # Фильтруем задачи с шаблонами
        with_templates = [t for t in pool if t.has_template]
        without_templates = [t for t in pool if not t.has_template]

        # Генерируем варианты для задач с шаблонами
        for i, task in enumerate(with_templates):
            if len(result) >= count:
                break

            try:
                from src.domain.llm_templatizer import (
                    templatize_via_llm,
                    TEMPLATABLE_TYPES,
                )

                if "exact_number" not in TEMPLATABLE_TYPES:
                    pass  # safety — always true
                tpl = templatize_via_llm(
                    task.problem_text, "", task.answer, "exact_number"
                )
                if tpl:
                    import random as _rng

                    r = _rng.Random(seed + i)
                    values = {}
                    for pname, pinfo in tpl.params.items():
                        if pinfo.value_type == "int":
                            values[pname] = r.randint(pinfo.min_val, pinfo.max_val)
                        else:
                            values[pname] = round(
                                r.uniform(pinfo.min_val, pinfo.max_val), 2
                            )
                    from sympy import sympify, N as sympy_N, parse_expr

                    text = tpl.text_template.format(**values)
                    answer_expr = parse_expr(tpl.answer_formula_text)
                    syms = {k: sympify(v) for k, v in values.items()}
                    res = float(sympy_N(answer_expr.subs(syms)))
                    answer_str = str(int(res)) if res == int(res) else f"{res:.4g}"
                    result.append(
                        TaskInfo(
                            task_id=str(uuid.uuid4()),
                            textbook_id=task.textbook_id,
                            paragraph=task.paragraph,
                            task_number=f"{task.task_number}v",
                            skill_id=task.skill_id,
                            difficulty=task.difficulty,
                            problem_text=text,
                            answer=answer_str,
                            template_id=task.template_id,
                            has_template=True,
                        )
                    )
                    continue
            except Exception:
                pass
            # Fallback на оригинал
            result.append(task)

        # Добиваем оригинальными
        for task in without_templates:
            if len(result) >= count:
                break
            result.append(task)

        return result[:count]

    def format_exam_for_print(
        self, exam: GeneratedExam, variant_index: int = 0, show_answers: bool = False
    ) -> str:
        """Форматирует тест для печати"""

        variant = exam.variants[variant_index]
        config = exam.config

        lines = [
            "=" * 60,
            f"        {config.title}",
            f"        Вариант {variant.variant_number}",
            "=" * 60,
            f"Время: {variant.time_minutes} мин.    Баллов: {variant.total_points}",
            "-" * 60,
            "",
        ]

        for i, task in enumerate(variant.tasks, 1):
            lines.append(f"Задача {i}. ({task.difficulty.value.upper()})")
            lines.append(task.problem_text)
            lines.append("")

            if show_answers:
                lines.append(f"   Ответ: {task.answer}")
                lines.append("")

        lines.extend(
            [
                "-" * 60,
                f"Учебник: {config.textbook_id}",
                f"Навыки: {', '.join(s for s in exam.skills_covered if s)}",
                "=" * 60,
            ]
        )

        return "\n".join(lines)

    def export_to_json(self, exam: GeneratedExam) -> str:
        """Экспортирует тест в JSON"""

        data = {
            "exam_id": exam.exam_id,
            "title": exam.config.title,
            "type": exam.config.exam_type.value,
            "created_at": exam.created_at.isoformat(),
            "skills_covered": list(exam.skills_covered),
            "variants": [],
        }

        for variant in exam.variants:
            var_data = {
                "variant_id": variant.variant_id,
                "variant_number": variant.variant_number,
                "total_points": variant.total_points,
                "time_minutes": variant.time_minutes,
                "tasks": [],
            }

            for task in variant.tasks:
                var_data["tasks"].append(
                    {
                        "task_id": task.task_id,
                        "task_number": task.task_number,
                        "skill_id": task.skill_id,
                        "has_core_mapping": task.has_core_mapping,
                        "difficulty": task.difficulty.value,
                        "problem_text": task.problem_text,
                        "answer": task.answer,
                    }
                )

            data["variants"].append(var_data)

        return json.dumps(data, ensure_ascii=False, indent=2)


# ============================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ
# ============================================================================

if __name__ == "__main__":
    generator = ExamGenerator()

    print("=" * 70)
    print("ТЕСТИРОВАНИЕ ГЕНЕРАТОРА ЭКЗАМЕНОВ (Модуль 2)")
    print("=" * 70)

    # ===== РЕЖИМ 1: Оригинальные задачи =====
    print("\n📚 РЕЖИМ 1: ОРИГИНАЛЬНЫЕ ЗАДАЧИ")
    print("-" * 50)

    config1 = ExamConfig(
        title="Контрольная работа: Уравнения",
        exam_type=ExamType.TEST,
        textbook_id="MAKARYCHEV_8",
        paragraphs=["1.1", "1.2"],
        tasks_count=5,
        time_minutes=45,
        variant_mode=VariantMode.ORIGINAL,
        variants_count=1,
    )

    exam1 = generator.generate_exam(config1)
    print(generator.format_exam_for_print(exam1, show_answers=True))

    # ===== РЕЖИМ 2: Перемешанные задачи =====
    print("\n🔀 РЕЖИМ 2: ПЕРЕМЕШАННЫЕ ЗАДАЧИ")
    print("-" * 50)

    config2 = ExamConfig(
        title="Самостоятельная работа",
        exam_type=ExamType.QUIZ,
        textbook_id="MAKARYCHEV_8",
        paragraphs=["1.1", "2.1"],
        tasks_count=4,
        time_minutes=20,
        variant_mode=VariantMode.SHUFFLE,
        variants_count=2,
    )

    exam2 = generator.generate_exam(config2)

    for i in range(len(exam2.variants)):
        print(f"\n--- Вариант {i + 1} ---")
        print(generator.format_exam_for_print(exam2, variant_index=i))

    # ===== РЕЖИМ 3: Генерация через шаблоны (дополнительная опция) =====
    print("\n⚙️  РЕЖИМ 3: ГЕНЕРАЦИЯ ЧЕРЕЗ ШАБЛОНЫ (доп. опция)")
    print("-" * 50)

    config3 = ExamConfig(
        title="Вариативная контрольная",
        exam_type=ExamType.TEST,
        textbook_id="MAKARYCHEV_8",
        paragraphs=["1.1"],
        tasks_count=3,
        time_minutes=30,
        variant_mode=VariantMode.TEMPLATE,
        variants_count=3,
    )

    exam3 = generator.generate_exam(config3)

    print("Сгенерировано 3 уникальных варианта с разными числами:")
    for i, variant in enumerate(exam3.variants):
        print(f"\n📋 Вариант {variant.variant_number}:")
        for task in variant.tasks:
            print(f"   • {task.problem_text}")
            print(f"     Ответ: {task.answer}")

    # ===== Статистика =====
    print("\n" + "=" * 70)
    print("📊 СТАТИСТИКА")
    print("=" * 70)

    print(f"\nТест 1: {exam1.config.title}")
    print(f"   Навыки: {exam1.skills_covered}")
    print(f"   Сложность: {exam1.difficulty_distribution}")

    print(f"\nТест 2: {exam2.config.title}")
    print(f"   Варианты: {len(exam2.variants)}")

    print(f"\nТест 3: {exam3.config.title}")
    print(f"   Варианты: {len(exam3.variants)}")
    print(f"   Режим: {exam3.config.variant_mode.value}")
