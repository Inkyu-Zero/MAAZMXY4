"""移动方向及动作编号对应关系。"""

from enum import Enum


class Direction(Enum):
    Right = "D"
    Down = "S"
    Left = "A"
    Up = "W"
    DownRight = "SD"
    DownLeft = "SA"
    UpRight = "WD"
    UpLeft = "WA"


def is_single_direction(direction: Direction) -> bool:
    """判断是否为单方向"""
    return len(direction.value) == 1


def is_vertical_direction(direction: Direction) -> bool:
    """判断是否为垂直方向"""
    return direction in [Direction.Up, Direction.Down]


def is_horizontal_direction(direction: Direction) -> bool:
    """判断是否为水平方向"""
    return direction in [Direction.Left, Direction.Right]


def is_reverse_direction(dir1: Direction, dir2: Direction) -> bool:
    """判断两个方向是否相反"""
    if dir1 == Direction.Right and dir2 == Direction.Left:
        return True
    elif dir1 == Direction.Left and dir2 == Direction.Right:
        return True
    elif dir1 == Direction.Up and dir2 == Direction.Down:
        return True
    elif dir1 == Direction.Down and dir2 == Direction.Up:
        return True
    elif dir1 == Direction.UpRight and dir2 == Direction.DownLeft:
        return True
    elif dir1 == Direction.DownLeft and dir2 == Direction.UpRight:
        return True
    elif dir1 == Direction.UpLeft and dir2 == Direction.DownRight:
        return True
    elif dir1 == Direction.DownRight and dir2 == Direction.UpLeft:
        return True
    else:
        return False


def get_horizontal_and_vertical_direction(direction: Direction) -> tuple:
    """获取水平和垂直方向"""
    if direction == Direction.Right:
        return Direction.Right, None
    elif direction == Direction.Left:
        return Direction.Left, None
    elif direction == Direction.Up:
        return None, Direction.Up
    elif direction == Direction.Down:
        return None, Direction.Down
    elif direction == Direction.UpRight:
        return Direction.Right, Direction.Up
    elif direction == Direction.UpLeft:
        return Direction.Left, Direction.Up
    elif direction == Direction.DownRight:
        return Direction.Right, Direction.Down
    elif direction == Direction.DownLeft:
        return Direction.Left, Direction.Down
    raise ValueError(f"Invalid direction: {direction}")


direction_list = [Direction.Right, Direction.Down, Direction.Left, Direction.Up,
                  Direction.DownRight, Direction.DownLeft, Direction.UpRight, Direction.UpLeft]

# 用于map中移动的偏移量
move_list = [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)]


def get_direction_vector(direction: Direction | None):
    """将实际按键方向转换为寻路使用的单位向量；停留没有方向偏好。"""
    return None if direction is None else move_list[direction_list.index(direction)]
