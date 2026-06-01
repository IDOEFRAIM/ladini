class ListNode:
    def __init__(self, value=0, next=None):
        self.value = value
        self.next = next

premier = ListNode(1)
deuxieme = ListNode(2)
troisieme = ListNode(3)
quartieme = ListNode(4)
premier.next = deuxieme
deuxieme.next = troisieme
troisieme.next = quartieme


def mul(head:ListNode) -> ListNode:
    if head is None:
        return None
    
    nombre = 0
    mul = 1
    current = head

    while current is not None:
        nombre += current.value * mul

        mul *= 10

        current = current.next
    
    return nombre


nombre = mul(premier)

#print(nombre)


def toNde(nombre):
    if nombre ==0:
        return ListNode(0)

    dummy = ListNode(0)
    current = dummy

    while nombre > 0:
        chiffre = nombre % 10
        current.next = ListNode(chiffre)
        nombre //= 10