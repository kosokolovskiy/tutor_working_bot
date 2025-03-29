from db_info.difference import DBAnalyzer



def ingestHomework(tasks_d: dict, date_: str, student_name):
    print('Inserting Data...')
    obj = DBAnalyzer()


    if tasks_d and date_:
        obj.ingestHomework(tasks_d, date_, student_name)
        print('Done!')


def doneOrNot(student_name: str, ):
    print('Start of Analysis...')
    obj = DBAnalyzer()
    return obj.findNotDone(student_name)

